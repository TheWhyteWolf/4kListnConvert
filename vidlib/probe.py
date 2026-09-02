"""ffprobe/ffmpeg discovery and metadata extraction."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from .models import VideoFile


class FFmpegMissing(RuntimeError):
    """Raised when the ffmpeg toolchain is not installed."""


def find_tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise FFmpegMissing(
            f"{name} not found on PATH. Install ffmpeg "
            "(Arch: sudo pacman -S ffmpeg, Debian/Ubuntu: sudo apt install ffmpeg)."
        )
    return path


def ffprobe_bin() -> str:
    return find_tool("ffprobe")


def ffmpeg_bin() -> str:
    return find_tool("ffmpeg")


def available_encoders() -> set[str]:
    """Video encoder names this ffmpeg build actually offers."""
    try:
        out = subprocess.run(
            [ffmpeg_bin(), "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=20, check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError, FFmpegMissing):
        return set()
    found = set()
    for line in out.splitlines():
        parts = line.split()
        # Encoder lines look like: " V....D libx265   libx265 H.265 / HEVC"
        if len(parts) >= 2 and parts[0].startswith("V") and len(parts[0]) == 6:
            found.add(parts[1])
    return found


_UNSET = {"unknown", "n/a", "none", "unspecified", "reserved", ""}


def _clean(value) -> str | None:
    """ffprobe writes literal 'unknown'/'N/A' rather than omitting a field."""
    if value is None:
        return None
    text = str(value).strip()
    return None if text.lower() in _UNSET else text


def _to_float(value) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and result not in (float("inf"), float("-inf")) else None


def _to_int(value) -> int | None:
    result = _to_float(value)
    return int(result) if result is not None else None


def _parse_fps(rate: str | None) -> float | None:
    """'24000/1001' -> 23.976."""
    if not rate or rate in ("0/0", "N/A"):
        return None
    if "/" in rate:
        num, _, den = rate.partition("/")
        num_f, den_f = _to_float(num), _to_float(den)
        if not num_f or not den_f:
            return None
        return num_f / den_f
    return _to_float(rate)


def probe_file(path: str | Path, timeout: float = 90.0) -> VideoFile:
    """Run ffprobe on one file and fold the result into a VideoFile.

    Never raises for a bad file: unreadable media come back with probe_error
    set so a broken file cannot abort a whole library scan.
    """
    path = os.fspath(path)
    try:
        stat = os.stat(path)
    except OSError as exc:
        return VideoFile(path=path, size=0, mtime=0.0, probe_error=str(exc))

    record = VideoFile(
        path=path,
        size=stat.st_size,
        mtime=stat.st_mtime,
        container=os.path.splitext(path)[1].lower().lstrip("."),
        probed_at=__import__("time").time(),
    )

    cmd = [
        ffprobe_bin(), "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", "-i", path,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        record.probe_error = "ffprobe timed out"
        return record
    except OSError as exc:
        record.probe_error = f"ffprobe failed: {exc}"
        return record

    if proc.returncode != 0 or not proc.stdout.strip():
        record.probe_error = (proc.stderr or "ffprobe returned no data").strip().splitlines()[-1][:200]
        return record

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        record.probe_error = f"unparseable ffprobe output: {exc}"
        return record

    fmt = data.get("format") or {}
    record.duration = _to_float(fmt.get("duration"))

    streams = data.get("streams") or []
    audio_bitrates: list[int] = []

    for stream in streams:
        kind = stream.get("codec_type")
        if kind == "video":
            # Cover art is stored as a video stream; ignore it.
            if stream.get("disposition", {}).get("attached_pic"):
                continue
            if record.vcodec is None:
                record.vcodec = _clean(stream.get("codec_name"))
                record.vprofile = _clean(stream.get("profile"))
                record.width = _to_int(stream.get("width"))
                record.height = _to_int(stream.get("height"))
                record.pix_fmt = _clean(stream.get("pix_fmt"))
                record.fps = _parse_fps(
                    stream.get("avg_frame_rate") or stream.get("r_frame_rate")
                )
                record.vbitrate = _to_int(stream.get("bit_rate"))
                record.color_transfer = _clean(stream.get("color_transfer"))
                record.color_primaries = _clean(stream.get("color_primaries"))
                record.color_space = _clean(stream.get("color_space"))
                for side in stream.get("side_data_list") or []:
                    if "dovi" in str(side.get("side_data_type", "")).lower():
                        record.has_dovi = True
        elif kind == "audio":
            record.n_audio += 1
            lang = (stream.get("tags") or {}).get("language")
            if lang and lang not in record.audio_langs:
                record.audio_langs.append(lang)
            rate = _to_int(stream.get("bit_rate"))
            if rate:
                audio_bitrates.append(rate)
            if record.acodec is None:
                record.acodec = _clean(stream.get("codec_name"))
                record.achannels = _to_int(stream.get("channels"))
                record.abitrate = rate
        elif kind == "subtitle":
            record.n_subs += 1
            lang = (stream.get("tags") or {}).get("language")
            if lang and lang not in record.sub_langs:
                record.sub_langs.append(lang)

    if audio_bitrates:
        # Scale up when only some tracks reported a bitrate, so the total is
        # not understated by tracks ffprobe could not measure.
        mean = sum(audio_bitrates) / len(audio_bitrates)
        record.abitrate_total = int(mean * record.n_audio)

    if record.vbitrate is None and record.duration:
        # Many containers omit per-stream bitrate; approximate the video stream
        # as whatever is left after the audio tracks.
        total = record.size * 8 / record.duration
        record.vbitrate = max(0, int(total - (record.abitrate_total or 0))) or None

    if record.vcodec is None:
        record.probe_error = record.probe_error or "no video stream"

    return record
