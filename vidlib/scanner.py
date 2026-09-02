# SPDX-License-Identifier: GPL-3.0-or-later
"""Recursive discovery of video files, with cached parallel probing."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from .db import Library
from .models import VIDEO_EXTENSIONS, VideoFile
from .probe import probe_file

# Directories that hold thumbnails, trash or NAS metadata rather than media.
DEFAULT_EXCLUDES = frozenset(
    {
        "@eadir", ".trash", ".trash-1000", "$recycle.bin", "lost+found",
        ".stfolder", ".stversions", "#recycle", ".git", "__pycache__",
    }
)


@dataclass
class ScanStats:
    found: int = 0
    probed: int = 0
    cached: int = 0
    errors: int = 0
    skipped_small: int = 0

    def __str__(self) -> str:
        parts = [f"{self.found} found", f"{self.probed} probed", f"{self.cached} cached"]
        if self.errors:
            parts.append(f"{self.errors} unreadable")
        if self.skipped_small:
            parts.append(f"{self.skipped_small} below size floor")
        return ", ".join(parts)


def walk_videos(
    root: str | Path,
    *,
    excludes: frozenset[str] = DEFAULT_EXCLUDES,
    include_hidden: bool = False,
    extensions: frozenset[str] = VIDEO_EXTENSIONS,
    follow_symlinks: bool = False,
) -> Iterator[os.DirEntry]:
    """Yield DirEntry for every video file under root.

    Uses scandir so size/mtime come from the directory read rather than an
    extra stat per file.
    """
    root = os.fspath(Path(root).expanduser())
    stack = [root]
    seen_dirs: set[tuple[int, int]] = set()
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    name = entry.name
                    try:
                        if entry.is_dir(follow_symlinks=follow_symlinks):
                            if name.lower() in excludes:
                                continue
                            if not include_hidden and name.startswith("."):
                                continue
                            if follow_symlinks:
                                # Guard against symlink loops.
                                stat = entry.stat()
                                key = (stat.st_dev, stat.st_ino)
                                if key in seen_dirs:
                                    continue
                                seen_dirs.add(key)
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=follow_symlinks):
                            if not include_hidden and name.startswith("."):
                                continue
                            if os.path.splitext(name)[1].lower() in extensions:
                                yield entry
                    except OSError:
                        continue
        except (PermissionError, FileNotFoundError, NotADirectoryError):
            continue


def scan(
    roots: list[str | Path],
    library: Library,
    *,
    workers: int = 0,
    force: bool = False,
    min_size: int = 0,
    include_hidden: bool = False,
    follow_symlinks: bool = False,
    progress: Callable[[int, int, str], None] | None = None,
) -> ScanStats:
    """Scan roots into the library, reusing cached probes where still valid.

    `progress(done, total, path)` is called as probing advances.
    """
    stats = ScanStats()
    todo: list[str] = []
    batch: list[VideoFile] = []

    for root in roots:
        for entry in walk_videos(
            root, include_hidden=include_hidden, follow_symlinks=follow_symlinks
        ):
            try:
                info = entry.stat()
            except OSError:
                continue
            if min_size and info.st_size < min_size:
                stats.skipped_small += 1
                continue
            stats.found += 1
            path = os.path.abspath(entry.path)
            if not force and library.is_fresh(path, info.st_size, info.st_mtime):
                stats.cached += 1
                continue
            todo.append(path)

    total = len(todo)
    if not total:
        if progress:
            progress(0, 0, "")
        return stats

    workers = workers or min(8, (os.cpu_count() or 4))
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(probe_file, path): path for path in todo}
        for future in as_completed(futures):
            path = futures[future]
            try:
                video = future.result()
            except Exception as exc:  # a probe must never kill the scan
                video = VideoFile(path=path, size=0, mtime=0.0, probe_error=str(exc))
            if video.probe_error:
                stats.errors += 1
            else:
                stats.probed += 1
            batch.append(video)
            done += 1
            if progress:
                progress(done, total, path)
            if len(batch) >= 200:
                library.put_many(batch)
                batch.clear()

    if batch:
        library.put_many(batch)
    return stats
