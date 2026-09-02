# SPDX-License-Identifier: GPL-3.0-or-later
"""Planning, running and verifying 4K -> 1080p conversions."""

from __future__ import annotations

import os
import re
import signal
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import trash
from .models import VideoFile
from .probe import ffmpeg_bin, probe_file
from .util import human_size

TMP_SUFFIX = ".vidlib-tmp"


# --- encoders ---------------------------------------------------------------


@dataclass(frozen=True)
class EncoderSpec:
    name: str            # ffmpeg encoder name
    label: str           # what to show a human
    quality_flag: str    # -crf for CPU encoders, -cq for NVENC
    default_quality: int
    presets: tuple[str, ...]
    default_preset: str
    ten_bit_pix_fmt: str
    eight_bit_pix_fmt: str = "yuv420p"
    hardware: bool = False
    notes: str = ""


ENCODERS: dict[str, EncoderSpec] = {
    "libx265": EncoderSpec(
        "libx265", "x265 / HEVC (CPU)", "-crf", 20,
        ("ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"),
        "medium", "yuv420p10le",
        notes="Best quality per byte; keeps HDR. Slow on CPU.",
    ),
    "libx264": EncoderSpec(
        "libx264", "x264 / H.264 (CPU)", "-crf", 20,
        ("ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"),
        "medium", "yuv420p10le",
        notes="Most compatible; ~1.5-2x larger than x265. HDR is tone-mapped.",
    ),
    "libsvtav1": EncoderSpec(
        "libsvtav1", "SVT-AV1 (CPU)", "-crf", 30,
        tuple(str(i) for i in range(1, 14)),
        "6", "yuv420p10le",
        notes="Smallest files; slower and needs a recent player.",
    ),
    "hevc_nvenc": EncoderSpec(
        "hevc_nvenc", "HEVC NVENC (GPU)", "-cq", 24,
        ("p1", "p2", "p3", "p4", "p5", "p6", "p7"), "p5", "p010le",
        hardware=True,
        notes="Fast, but needs Maxwell GM206 or newer. Lower quality per byte.",
    ),
    "h264_nvenc": EncoderSpec(
        "h264_nvenc", "H.264 NVENC (GPU)", "-cq", 23,
        ("p1", "p2", "p3", "p4", "p5", "p6", "p7"), "p5", "yuv420p",
        hardware=True,
        notes="Fastest option; noticeably larger files at equal quality.",
    ),
}

DEFAULT_ENCODER = "libx265"


# --- output naming ----------------------------------------------------------

_RES_TOKEN = re.compile(r"(?<![a-z0-9])(2160p|1440p|4320p|4k|uhd|8k|3840x2160|4096x2160)(?![a-z0-9])", re.I)
_HDR_TOKEN = re.compile(
    r"(?<![a-z0-9])(hdr10\+?|hdr|dolby[.\s_-]?vision|dovi|dv|hlg)(?![a-z0-9])", re.I
)
_CODEC_TOKEN = re.compile(r"(?<![a-z0-9])(x26[45]|h[.\s_-]?26[45]|hevc|avc1?|av1|xvid|divx)(?![a-z0-9])", re.I)

_CODEC_LABEL = {
    "libx265": "x265", "libx264": "x264", "libsvtav1": "AV1",
    "hevc_nvenc": "x265", "h264_nvenc": "x264",
}


def _collapse_repeats(text: str, token: str) -> str:
    """Fold "1080p.1080p" or "SDR SDR" down to a single occurrence."""
    escaped = re.escape(token)
    pattern = rf"(?<![a-z0-9]){escaped}(?:[._\s-]+{escaped})+(?![a-z0-9])"
    return re.sub(pattern, token, text, flags=re.I)


def output_name(
    source: Path,
    target_height: int = 1080,
    *,
    encoder: str = DEFAULT_ENCODER,
    tonemapped: bool = False,
    retag_codec: bool = True,
    container: str | None = None,
) -> Path:
    """Derive the converted file's name from the source's.

    Rewrites resolution/HDR/codec tags in the filename so the result is not
    mislabelled as 2160p HDR after being downscaled.
    """
    stem = source.stem
    suffix = f".{container.lstrip('.')}" if container else source.suffix
    label = f"{target_height}p"

    new_stem, count = _RES_TOKEN.subn(label, stem)
    if count == 0 and not re.search(rf"(?<![a-z0-9]){label}(?![a-z0-9])", stem, re.I):
        new_stem = f"{stem}.{label}"

    if tonemapped:
        new_stem = _HDR_TOKEN.sub("SDR", new_stem)
    if retag_codec and encoder in _CODEC_LABEL:
        new_stem = _CODEC_TOKEN.sub(_CODEC_LABEL[encoder], new_stem)

    # A name like "2160p.UHD" yields two copies of the label; keep one.
    for token in {label, "SDR", _CODEC_LABEL.get(encoder, "")}:
        if token:
            new_stem = _collapse_repeats(new_stem, token)
    # Collapse separator runs left behind by substitutions.
    new_stem = re.sub(r"\.{2,}", ".", new_stem).strip(". ")
    candidate = source.with_name(new_stem + suffix)

    if candidate.resolve() == source.resolve():
        candidate = source.with_name(f"{new_stem}.converted{suffix}")
    counter = 1
    while candidate.exists():
        candidate = source.with_name(f"{new_stem}.{counter}{suffix}")
        counter += 1
    return candidate


def temp_path_for(destination: Path) -> Path:
    """Hidden temp file beside the destination, so rename stays atomic."""
    return destination.with_name(f".{destination.stem}{TMP_SUFFIX}{destination.suffix}")


# --- planning ---------------------------------------------------------------


@dataclass
class ConversionPlan:
    source: VideoFile
    destination: Path
    temp: Path
    encoder: str = DEFAULT_ENCODER
    quality: int = 20
    preset: str = "medium"
    target_height: int = 1080
    tonemap: bool = False
    preserve_hdr: bool = False
    copy_audio: bool = True
    audio_codec: str = "aac"
    audio_bitrate: str = "192k"
    copy_subs: bool = True
    disposal: str = trash.TRASH
    quarantine_dir: str | None = None
    extra_args: list[str] = field(default_factory=list)

    @property
    def spec(self) -> EncoderSpec:
        return ENCODERS[self.encoder]

    @property
    def estimated_output(self) -> int | None:
        return self.source.estimated_output_size(
            self.encoder, self.target_height, self.copy_audio
        )


def decide_hdr_handling(
    video: VideoFile, encoder: str, tonemap: str = "auto"
) -> tuple[bool, bool]:
    """Return (tonemap, preserve_hdr) for this source and encoder.

    'auto' keeps HDR when the encoder can carry it in 10-bit, and tone-maps to
    SDR otherwise - an 8-bit HDR encode looks washed out and grey.
    """
    if not video.is_hdr:
        return False, False
    if tonemap == "always":
        return True, False
    if tonemap == "never":
        return False, True
    can_keep_hdr = encoder in ("libx265", "hevc_nvenc", "libsvtav1")
    return (False, True) if can_keep_hdr else (True, False)


def plan_conversion(
    video: VideoFile,
    *,
    encoder: str = DEFAULT_ENCODER,
    quality: int | None = None,
    preset: str | None = None,
    target_height: int = 1080,
    tonemap: str = "auto",
    copy_audio: bool = True,
    audio_codec: str = "aac",
    audio_bitrate: str = "192k",
    copy_subs: bool = True,
    disposal: str = trash.TRASH,
    quarantine_dir: str | None = None,
    container: str | None = None,
    retag_codec: bool = True,
) -> ConversionPlan:
    if encoder not in ENCODERS:
        raise ValueError(f"unknown encoder {encoder!r} (choose from {', '.join(ENCODERS)})")
    spec = ENCODERS[encoder]
    do_tonemap, keep_hdr = decide_hdr_handling(video, encoder, tonemap)
    destination = output_name(
        Path(video.path), target_height, encoder=encoder,
        tonemapped=do_tonemap, retag_codec=retag_codec, container=container,
    )
    return ConversionPlan(
        source=video,
        destination=destination,
        temp=temp_path_for(destination),
        encoder=encoder,
        quality=spec.default_quality if quality is None else quality,
        preset=preset or spec.default_preset,
        target_height=target_height,
        tonemap=do_tonemap,
        preserve_hdr=keep_hdr,
        copy_audio=copy_audio,
        audio_codec=audio_codec,
        audio_bitrate=audio_bitrate,
        copy_subs=copy_subs,
        disposal=disposal,
        quarantine_dir=quarantine_dir,
    )


# --- command construction ---------------------------------------------------


def build_filters(plan: ConversionPlan) -> str:
    """Video filter chain: downscale, then tone-map if we are dropping HDR."""
    height = plan.target_height
    width = round(height * 16 / 9 / 2) * 2
    # min() against the input dimensions prevents upscaling a smaller source.
    chain = [
        f"scale=w='min({width},iw)':h='min({height},ih)'"
        ":force_original_aspect_ratio=decrease:force_divisible_by=2"
    ]
    if plan.tonemap:
        chain += [
            "zscale=t=linear:npl=100",
            "format=gbrpf32le",
            "zscale=p=bt709",
            "tonemap=tonemap=hable:desat=0",
            "zscale=t=bt709:m=bt709:r=tv",
            "format=yuv420p",
        ]
    return ",".join(chain)


def build_command(plan: ConversionPlan, *, drop_subs: bool = False) -> list[str]:
    """Assemble the full ffmpeg invocation for a plan."""
    spec = plan.spec
    src = plan.source
    cmd = [
        ffmpeg_bin(), "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
        "-i", src.path,
        # Explicit maps: take one video stream (skipping cover art), plus every
        # audio, subtitle and attachment stream that exists.
        "-map", "0:v:0", "-map", "0:a?",
    ]
    if plan.copy_subs and not drop_subs:
        cmd += ["-map", "0:s?"]
    cmd += ["-map", "0:t?", "-map_chapters", "0", "-map_metadata", "0"]

    cmd += ["-c", "copy", "-c:v", spec.name, spec.quality_flag, str(plan.quality)]
    cmd += ["-preset", plan.preset]
    cmd += ["-vf", build_filters(plan)]

    # HDR requires 10-bit. Otherwise follow the source, except for H.264:
    # 10-bit H.264 is poorly supported by hardware players, and compatibility
    # is the whole reason to pick that encoder.
    wants_ten_bit = plan.preserve_hdr or (src.bit_depth or 8) >= 10
    if wants_ten_bit and not plan.preserve_hdr and spec.name in ("libx264", "h264_nvenc"):
        wants_ten_bit = False
    cmd += ["-pix_fmt", spec.ten_bit_pix_fmt if wants_ten_bit else spec.eight_bit_pix_fmt]

    if plan.preserve_hdr:
        cmd += [
            "-color_primaries", src.color_primaries or "bt2020",
            "-color_trc", src.color_transfer or "smpte2084",
            "-colorspace", src.color_space or "bt2020nc",
        ]
        if spec.name == "libx265":
            hdr_params = (
                "hdr-opt=1:repeat-headers=1:"
                f"colorprim={src.color_primaries or 'bt2020'}:"
                f"transfer={src.color_transfer or 'smpte2084'}:"
                f"colormatrix={src.color_space or 'bt2020nc'}"
            )
            cmd += ["-x265-params", hdr_params]
    elif plan.tonemap:
        cmd += ["-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709"]

    if spec.name == "libx265" and "-x265-params" not in cmd:
        cmd += ["-x265-params", "log-level=error"]

    if not plan.copy_audio:
        cmd += ["-c:a", plan.audio_codec, "-b:a", plan.audio_bitrate]

    cmd += plan.extra_args
    cmd += ["-progress", "pipe:1", "-nostats", str(plan.temp)]
    return cmd


# --- execution --------------------------------------------------------------


@dataclass
class ConversionResult:
    plan: ConversionPlan
    ok: bool
    output: Path | None = None
    output_size: int = 0
    elapsed: float = 0.0
    disposal: str = ""
    error: str = ""
    cancelled: bool = False

    @property
    def saved(self) -> int:
        return max(0, self.plan.source.size - self.output_size) if self.ok else 0


@dataclass
class Progress:
    percent: float
    seconds_done: float
    total_seconds: float
    fps: float
    speed: float
    out_size: int

    @property
    def eta_seconds(self) -> float | None:
        remaining = max(0.0, self.total_seconds - self.seconds_done)
        return remaining / self.speed if self.speed > 0 else None


def _parse_progress(line: str, state: dict) -> None:
    key, _, value = line.strip().partition("=")
    if key and value:
        state[key] = value


def verify_output(plan: ConversionPlan, output: Path) -> str | None:
    """Sanity-check an encode. Returns an error string, or None when good.

    The source is only disposed of after this passes, so it is deliberately
    strict about duration and dimensions.
    """
    if not output.exists():
        return "output file was not created"
    size = output.stat().st_size
    if size == 0:
        return "output file is empty"

    probe = probe_file(output)
    if probe.probe_error:
        return f"output is unreadable: {probe.probe_error}"
    if not probe.vcodec:
        return "output has no video stream"
    if probe.height and probe.height > plan.target_height + 8:
        return f"output height {probe.height} exceeds target {plan.target_height}"

    source_duration = plan.source.duration
    if source_duration and probe.duration:
        drift = abs(source_duration - probe.duration)
        allowed = max(2.0, source_duration * 0.01)
        if drift > allowed:
            return (
                f"duration mismatch: source {source_duration:.1f}s vs "
                f"output {probe.duration:.1f}s"
            )
    return None


def run_conversion(
    plan: ConversionPlan,
    *,
    progress: Callable[[Progress], None] | None = None,
    dry_run: bool = False,
    allow_larger: bool = False,
    should_cancel: Callable[[], bool] | None = None,
) -> ConversionResult:
    """Encode one file, verify it, then dispose of the source.

    The source is never touched unless the encode verified successfully.
    """
    started = time.monotonic()
    if dry_run:
        return ConversionResult(
            plan=plan, ok=True, output=plan.destination,
            output_size=plan.estimated_output or 0, disposal="(dry run)",
        )

    plan.temp.parent.mkdir(parents=True, exist_ok=True)
    plan.temp.unlink(missing_ok=True)

    def _execute(cmd: list[str]) -> tuple[int, str, bool]:
        state: dict[str, str] = {}
        total = plan.source.duration or 0.0
        was_cancelled = False
        stderr = ""
        # The context manager closes the pipes even if we break out early.
        with subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        ) as proc:
            try:
                assert proc.stdout is not None
                for line in proc.stdout:
                    _parse_progress(line, state)
                    if should_cancel and should_cancel():
                        was_cancelled = True
                        proc.send_signal(signal.SIGINT)
                        break
                    if line.startswith("progress=") and progress:
                        micros = float(
                            state.get("out_time_us") or state.get("out_time_ms") or 0
                        )
                        done = micros / 1_000_000
                        progress(
                            Progress(
                                percent=min(100.0, done / total * 100) if total else 0.0,
                                seconds_done=done,
                                total_seconds=total,
                                fps=float(state.get("fps") or 0) or 0.0,
                                speed=float((state.get("speed") or "0x").rstrip("x") or 0),
                                out_size=int(state.get("total_size") or 0),
                            )
                        )
            finally:
                try:
                    stderr = (proc.stderr.read() if proc.stderr else "") or ""
                except (OSError, ValueError):
                    stderr = ""
                code = proc.wait()
        return code, stderr.strip(), was_cancelled

    cmd = build_command(plan)
    code, stderr, cancelled = _execute(cmd)

    # Copying subtitles into a container that cannot hold them is a common,
    # clearly-diagnosable failure - retry once without them.
    if code != 0 and not cancelled and plan.copy_subs and "subtitle" in stderr.lower():
        plan.temp.unlink(missing_ok=True)
        code, stderr, cancelled = _execute(build_command(plan, drop_subs=True))
        if code == 0:
            stderr = "note: subtitles were dropped (container cannot store them)"

    elapsed = time.monotonic() - started

    if cancelled:
        plan.temp.unlink(missing_ok=True)
        return ConversionResult(plan=plan, ok=False, elapsed=elapsed,
                                cancelled=True, error="cancelled")
    if code != 0:
        plan.temp.unlink(missing_ok=True)
        message = stderr.splitlines()[-1] if stderr else f"ffmpeg exited {code}"
        return ConversionResult(plan=plan, ok=False, elapsed=elapsed, error=message[:400])

    problem = verify_output(plan, plan.temp)
    if problem:
        plan.temp.unlink(missing_ok=True)
        return ConversionResult(plan=plan, ok=False, elapsed=elapsed,
                                error=f"verification failed: {problem}")

    output_size = plan.temp.stat().st_size
    if output_size >= plan.source.size and not allow_larger:
        plan.temp.unlink(missing_ok=True)
        return ConversionResult(
            plan=plan, ok=False, elapsed=elapsed,
            error=(
                f"output ({human_size(output_size)}) is not smaller than the "
                f"source ({human_size(plan.source.size)}); source kept"
            ),
        )

    try:
        os.replace(plan.temp, plan.destination)
    except OSError as exc:
        plan.temp.unlink(missing_ok=True)
        return ConversionResult(plan=plan, ok=False, elapsed=elapsed,
                                error=f"cannot finalise output: {exc}")

    try:
        disposal = trash.dispose(
            plan.source.path, plan.disposal, quarantine_dir=plan.quarantine_dir
        )
    except trash.DisposalError as exc:
        return ConversionResult(
            plan=plan, ok=True, output=plan.destination, output_size=output_size,
            elapsed=elapsed, disposal=f"source kept ({exc})",
        )

    return ConversionResult(
        plan=plan, ok=True, output=plan.destination, output_size=output_size,
        elapsed=elapsed, disposal=disposal,
    )
