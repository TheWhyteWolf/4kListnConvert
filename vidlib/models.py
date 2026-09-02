"""The VideoFile record and everything derived from it."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path

VIDEO_EXTENSIONS = frozenset(
    """.mkv .mp4 .m4v .avi .mov .wmv .flv .webm .ts .m2ts .mts .vob .mpg .mpeg
       .divx .ogm .rm .rmvb .asf .3gp .m2v .mxf .f4v""".split()
)

# Ordered best-to-worst so groups sort sensibly.
RESOLUTION_ORDER = ["4320p", "2160p", "1440p", "1080p", "720p", "576p", "480p", "SD", "unknown"]

HDR_SDR = "SDR"


def classify_resolution(width: int | None, height: int | None) -> str:
    """Bucket a frame size into a marketing resolution name.

    Uses the larger of the real height and the height a 16:9 frame of this
    width would have, so letterboxed scope content (3840x1600) is still 2160p.
    """
    if not width or not height:
        return "unknown"
    effective = max(height, round(width * 9 / 16))
    if effective >= 3200:
        return "4320p"
    if effective >= 1700:
        return "2160p"
    if effective >= 1250:
        return "1440p"
    if effective >= 850:
        return "1080p"
    if effective >= 620:
        return "720p"
    if effective >= 520:
        return "576p"
    if effective >= 380:
        return "480p"
    return "SD"


def classify_hdr(
    transfer: str | None,
    primaries: str | None,
    has_dovi: bool,
    space: str | None = None,
    bit_depth: int | None = None,
) -> str:
    """Name the dynamic-range flavour from colour metadata.

    The transfer function is the authoritative signal, but plenty of real
    files carry only a partial tag set, so a wide-gamut 10-bit stream with no
    transfer is reported as generic HDR rather than silently as SDR.
    """
    if has_dovi:
        return "DolbyVision"
    transfer = (transfer or "").lower()
    if transfer in ("smpte2084", "smpte st 2084"):
        return "HDR10"
    if transfer in ("arib-std-b67", "arib_std_b67", "hlg"):
        return "HLG"
    wide_gamut = (primaries or "").lower().startswith("bt2020") or (
        space or ""
    ).lower().startswith("bt2020")
    if wide_gamut and (bit_depth or 8) >= 10:
        return "HDR"
    return HDR_SDR


def bit_depth_from_pix_fmt(pix_fmt: str | None) -> int | None:
    """yuv420p10le -> 10, p010le -> 10, yuv420p -> 8."""
    if not pix_fmt:
        return None
    # Covers both "yuv420p10le" and the hardware "p010le"/"p016le" family,
    # where the depth is written with a leading zero.
    match = re.search(r"p(\d{1,3})(?:le|be)?$", pix_fmt)
    if match:
        depth = int(match.group(1))
        return depth if depth else None
    if pix_fmt.startswith(("yuv", "gbr", "nv1", "nv2", "gray", "rgb", "bgr")):
        return 8
    return None


# Rough bits-per-pixel-per-frame yields at a visually-transparent CRF, used only
# to estimate output size before encoding. Real results vary with grain/motion.
_BPP_BY_ENCODER = {
    "libx265": 0.075,
    "libx264": 0.130,
    "libsvtav1": 0.060,
    "hevc_nvenc": 0.110,
    "h264_nvenc": 0.180,
}

_TITLE_NOISE = re.compile(
    r"\b(2160p|1080p|720p|480p|4k|uhd|hdr10\+?|hdr|hlg|dv|dolby ?vision|sdr|"
    r"x ?26[45]|h ?26[45]|hevc|avc|av1|vp9|xvid|divx|"
    r"bluray|blu-ray|bdrip|brrip|bdremux|remux|web-?dl|web-?rip|webrip|hdtv|dvdrip|"
    r"10 ?bit|8 ?bit|dts-?hd|dts-?x|dts|truehd|atmos|ddp?|eac3|ac3|aac|opus|flac|"
    r"ma|5 ?1|7 ?1|2 ?0|repack|proper|extended|remastered|imax|"
    r"amzn|nf|hmax|dsnp|atvp|itunes)\b",
    re.I,
)
_YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")


@dataclass(slots=True)
class VideoFile:
    """One video file on disk plus its probed metadata."""

    path: str
    size: int
    mtime: float
    # Container / probe status
    container: str = ""
    duration: float | None = None
    probe_error: str | None = None
    # Video stream
    vcodec: str | None = None
    vprofile: str | None = None
    width: int | None = None
    height: int | None = None
    pix_fmt: str | None = None
    fps: float | None = None
    vbitrate: int | None = None
    color_transfer: str | None = None
    color_primaries: str | None = None
    color_space: str | None = None
    has_dovi: bool = False
    # Audio / subtitles
    acodec: str | None = None
    achannels: int | None = None
    abitrate: int | None = None
    abitrate_total: int | None = None
    audio_langs: list[str] = field(default_factory=list)
    sub_langs: list[str] = field(default_factory=list)
    n_audio: int = 0
    n_subs: int = 0
    # Bookkeeping
    probed_at: float = 0.0
    converted_from: str | None = None

    # --- derived ------------------------------------------------------------

    @property
    def p(self) -> Path:
        return Path(self.path)

    @property
    def name(self) -> str:
        return os.path.basename(self.path)

    @property
    def directory(self) -> str:
        return os.path.dirname(self.path)

    @property
    def resolution(self) -> str:
        return classify_resolution(self.width, self.height)

    @property
    def dimensions(self) -> str:
        if not self.width or not self.height:
            return "-"
        return f"{self.width}x{self.height}"

    @property
    def bit_depth(self) -> int | None:
        return bit_depth_from_pix_fmt(self.pix_fmt)

    @property
    def hdr(self) -> str:
        return classify_hdr(
            self.color_transfer,
            self.color_primaries,
            self.has_dovi,
            self.color_space,
            self.bit_depth,
        )

    @property
    def is_hdr(self) -> bool:
        return self.hdr != HDR_SDR

    @property
    def is_4k(self) -> bool:
        return self.resolution in ("2160p", "4320p")

    @property
    def encode(self) -> str:
        """Human label for the video encode, e.g. 'hevc 10bit'."""
        if not self.vcodec:
            return "unknown"
        depth = self.bit_depth
        parts = [self.vcodec]
        if depth and depth != 8:
            parts.append(f"{depth}bit")
        return " ".join(parts)

    @property
    def overall_bitrate(self) -> float | None:
        """Whole-file bitrate in bits/sec, derived from size and duration."""
        if not self.duration or self.duration <= 0:
            return None
        return self.size * 8 / self.duration

    @property
    def bits_per_pixel(self) -> float | None:
        """Video bits per pixel per frame - the density knob for 'bloat'."""
        rate = self.vbitrate or self.overall_bitrate
        if not rate or not self.width or not self.height or not self.fps:
            return None
        pixels_per_sec = self.width * self.height * self.fps
        if pixels_per_sec <= 0:
            return None
        return rate / pixels_per_sec

    @property
    def audio_summary(self) -> str:
        if not self.acodec:
            return "-"
        layout = {1: "mono", 2: "2.0", 6: "5.1", 8: "7.1"}.get(
            self.achannels or 0, f"{self.achannels}ch" if self.achannels else ""
        )
        extra = f" +{self.n_audio - 1}" if self.n_audio > 1 else ""
        return f"{self.acodec} {layout}{extra}".strip()

    @property
    def title_key(self) -> str:
        """Normalised title used to spot the same film at several resolutions."""
        stem = os.path.splitext(self.name)[0]
        stem = re.sub(r"[._]+", " ", stem)
        # Flatten brackets but keep what is inside them, so a parenthesised
        # "(2017)" still registers as the release year.
        stem = re.sub(r"[\[\](){}]", " ", stem)
        # The year is conventionally the last one before the quality tags, so a
        # title that is itself a number ("Blade Runner 2049") is not mistaken.
        years = list(_YEAR_RE.finditer(stem))
        year = years[-1] if years else None
        # Cut at the year first: dropping noise tags afterwards would shift the
        # match offsets out from under us.
        stem = stem[: year.start()] if year else stem
        stem = _TITLE_NOISE.sub(" ", stem)
        stem = re.sub(r"[^a-z0-9 ]+", " ", stem.lower())
        stem = re.sub(r"\s+", " ", stem).strip()
        return f"{stem} ({year.group(1)})" if year else stem

    def estimated_output_size(
        self, encoder: str = "libx265", target_height: int = 1080, copy_audio: bool = True
    ) -> int | None:
        """Predict the converted file's size in bytes. Estimate only."""
        if not self.duration or not self.width or not self.height:
            return None
        scale = min(1.0, target_height / self.height)
        out_w = max(2, int(self.width * scale))
        out_h = max(2, int(self.height * scale))
        fps = self.fps or 24.0
        bpp = _BPP_BY_ENCODER.get(encoder, 0.09)
        video_bits = out_w * out_h * fps * bpp
        if copy_audio:
            # Prefer the real summed bitrate; fall back to a lossy-track guess.
            audio_bits = self.abitrate_total or (self.abitrate or 0) or 0
            if not audio_bits:
                audio_bits = 640_000 * max(1, self.n_audio)
        else:
            audio_bits = 160_000 * max(1, self.n_audio)
        total = (video_bits + audio_bits) * self.duration / 8
        # Re-encoding should never be predicted to grow the file.
        return int(min(total, self.size))

    def estimated_saving(self, encoder: str = "libx265", target_height: int = 1080) -> int:
        out = self.estimated_output_size(encoder, target_height)
        return max(0, self.size - out) if out is not None else 0

    # --- serialisation ------------------------------------------------------

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))

    @classmethod
    def from_json(cls, blob: str) -> "VideoFile":
        """Tolerant of records written by an older or newer schema."""
        data = json.loads(blob)
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})
