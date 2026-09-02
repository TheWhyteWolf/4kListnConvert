# SPDX-License-Identifier: GPL-3.0-or-later
"""Grouping, filtering and duplicate detection over a set of VideoFiles."""

from __future__ import annotations

import fnmatch
import os
from collections import defaultdict
from dataclasses import dataclass, field

from .models import RESOLUTION_ORDER, VideoFile

# --- group keys -------------------------------------------------------------


def _size_bucket(video: VideoFile) -> str:
    gigabytes = video.size / 1024**3
    for limit, label in (
        (1, "< 1 GB"), (2, "1-2 GB"), (5, "2-5 GB"), (10, "5-10 GB"),
        (20, "10-20 GB"), (40, "20-40 GB"),
    ):
        if gigabytes < limit:
            return label
    return "40 GB+"


def _efficiency_bucket(video: VideoFile) -> str:
    """How many bits each pixel costs - the signal for a bloated encode."""
    bpp = video.bits_per_pixel
    if bpp is None:
        return "unknown"
    for limit, label in (
        (0.04, "very efficient"), (0.08, "efficient"),
        (0.15, "normal"), (0.30, "heavy"),
    ):
        if bpp < limit:
            return label
    return "very heavy"


def _fps_bucket(video: VideoFile) -> str:
    if not video.fps:
        return "unknown"
    for target, label in ((23.5, "24p"), (24.5, "24p"), (25.5, "25p"),
                          (30.5, "30p"), (50.5, "50p"), (60.5, "60p")):
        if video.fps < target:
            return label
    return f"{video.fps:.0f}p"


def _year(video: VideoFile) -> str:
    key = video.title_key
    return key[-5:-1] if key.endswith(")") and len(key) >= 6 else "unknown"


GROUPERS: dict[str, tuple[str, callable]] = {
    "directory": ("Directory", lambda v: v.directory or "/"),
    "resolution": ("Resolution", lambda v: v.resolution),
    "codec": ("Codec", lambda v: v.vcodec or "unknown"),
    "encode": ("Encode", lambda v: v.encode),
    "container": ("Container", lambda v: f".{v.container}" if v.container else "unknown"),
    "hdr": ("Dynamic range", lambda v: v.hdr),
    "depth": ("Bit depth", lambda v: f"{v.bit_depth}-bit" if v.bit_depth else "unknown"),
    "audio": ("Audio", lambda v: v.acodec or "none"),
    "fps": ("Frame rate", _fps_bucket),
    "size": ("File size", _size_bucket),
    "efficiency": ("Encode efficiency", _efficiency_bucket),
    "year": ("Year", _year),
    "none": ("All files", lambda v: "All files"),
}

_SIZE_BUCKET_ORDER = ["< 1 GB", "1-2 GB", "2-5 GB", "5-10 GB", "10-20 GB", "20-40 GB", "40 GB+"]
_EFFICIENCY_ORDER = ["very efficient", "efficient", "normal", "heavy", "very heavy", "unknown"]


def group_key_order(key: str, names: list[str]) -> list[str]:
    """Order group names meaningfully rather than alphabetically."""
    if key == "resolution":
        rank = {name: i for i, name in enumerate(RESOLUTION_ORDER)}
        return sorted(names, key=lambda n: (rank.get(n, len(rank)), n))
    if key == "size":
        rank = {name: i for i, name in enumerate(_SIZE_BUCKET_ORDER)}
        return sorted(names, key=lambda n: (rank.get(n, 99), n))
    if key == "efficiency":
        rank = {name: i for i, name in enumerate(_EFFICIENCY_ORDER)}
        return sorted(names, key=lambda n: (rank.get(n, 99), n))
    if key == "year":
        return sorted(names, reverse=True)
    return sorted(names, key=str.lower)


def group_files(videos: list[VideoFile], key: str) -> list[tuple[str, list[VideoFile]]]:
    """Bucket videos by a named group key, in a sensible display order."""
    if key not in GROUPERS:
        raise ValueError(f"unknown group key: {key!r} (choose from {', '.join(GROUPERS)})")
    fn = GROUPERS[key][1]
    buckets: dict[str, list[VideoFile]] = defaultdict(list)
    for video in videos:
        buckets[fn(video)].append(video)
    for group in buckets.values():
        group.sort(key=lambda v: (-v.size, v.name.lower()))
    return [(name, buckets[name]) for name in group_key_order(key, list(buckets))]


# --- filtering --------------------------------------------------------------


@dataclass
class Filter:
    """Declarative filter over the library. Unset fields are ignored."""

    resolutions: set[str] = field(default_factory=set)
    codecs: set[str] = field(default_factory=set)
    containers: set[str] = field(default_factory=set)
    hdr: set[str] = field(default_factory=set)
    only_4k: bool = False
    only_hdr: bool = False
    exclude_hdr: bool = False
    min_size: int = 0
    max_size: int = 0
    min_bitrate: float = 0.0
    min_bpp: float = 0.0
    pattern: str = ""
    under: str = ""
    include_errors: bool = False

    def matches(self, video: VideoFile) -> bool:
        if video.probe_error and not self.include_errors:
            return False
        if self.only_4k and not video.is_4k:
            return False
        if self.resolutions and video.resolution not in self.resolutions:
            return False
        if self.codecs and (video.vcodec or "").lower() not in self.codecs:
            return False
        if self.containers and video.container.lower() not in self.containers:
            return False
        if self.hdr and video.hdr not in self.hdr:
            return False
        if self.only_hdr and not video.is_hdr:
            return False
        if self.exclude_hdr and video.is_hdr:
            return False
        if self.min_size and video.size < self.min_size:
            return False
        if self.max_size and video.size > self.max_size:
            return False
        if self.min_bitrate:
            rate = video.overall_bitrate or 0
            if rate < self.min_bitrate:
                return False
        if self.min_bpp:
            bpp = video.bits_per_pixel
            if bpp is None or bpp < self.min_bpp:
                return False
        if self.pattern:
            needle = self.pattern.lower()
            haystack = video.path.lower()
            if not fnmatch.fnmatch(haystack, needle) and needle not in haystack:
                return False
        if self.under:
            root = os.path.abspath(os.path.expanduser(self.under))
            if not (video.path == root or video.path.startswith(root.rstrip("/") + "/")):
                return False
        return True

    def apply(self, videos: list[VideoFile]) -> list[VideoFile]:
        return [v for v in videos if self.matches(v)]


# --- duplicates -------------------------------------------------------------


@dataclass
class DuplicateSet:
    """The same title present at more than one resolution or encode."""

    title: str
    files: list[VideoFile]

    @property
    def reclaimable(self) -> int:
        """Bytes freed by keeping only the highest-resolution copy."""
        return sum(f.size for f in self.files[1:])

    @property
    def resolutions(self) -> list[str]:
        seen: list[str] = []
        for f in self.files:
            if f.resolution not in seen:
                seen.append(f.resolution)
        return seen


def find_duplicates(
    videos: list[VideoFile], *, require_distinct_resolution: bool = True
) -> list[DuplicateSet]:
    """Find titles stored more than once, best copy first.

    Titles are matched on a normalised filename, so release-group tags and
    punctuation differences do not hide a duplicate.
    """
    rank = {name: i for i, name in enumerate(RESOLUTION_ORDER)}
    buckets: dict[str, list[VideoFile]] = defaultdict(list)
    for video in videos:
        if video.probe_error:
            continue
        key = video.title_key
        if key:
            buckets[key].append(video)

    results: list[DuplicateSet] = []
    for title, group in buckets.items():
        if len(group) < 2:
            continue
        if require_distinct_resolution and len({v.resolution for v in group}) < 2:
            continue
        group.sort(key=lambda v: (rank.get(v.resolution, 99), -v.size))
        results.append(DuplicateSet(title=title, files=group))
    results.sort(key=lambda d: -d.reclaimable)
    return results


def display_group_name(key: str, name: str, roots: list[str] | None = None) -> str:
    """Shorten a group label for display.

    Directory keys are absolute paths, which are unique but unreadable in a
    narrow column, so they are shown relative to their scan root.
    """
    if key != "directory" or not roots:
        return name
    best = ""
    for root in roots:
        if (name == root or name.startswith(root.rstrip("/") + "/")) and len(root) > len(best):
            best = root
    if not best:
        return name
    relative = os.path.relpath(name, best)
    label = os.path.basename(best.rstrip("/")) or best
    return label if relative == "." else f"{label}/{relative}"


def summarize(videos: list[VideoFile]) -> dict[str, object]:
    """Headline totals for a set of files."""
    total_size = sum(v.size for v in videos)
    fourk = [v for v in videos if v.is_4k]
    return {
        "count": len(videos),
        "size": total_size,
        "duration": sum(v.duration or 0 for v in videos),
        "count_4k": len(fourk),
        "size_4k": sum(v.size for v in fourk),
        "errors": sum(1 for v in videos if v.probe_error),
    }
