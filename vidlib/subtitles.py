# SPDX-License-Identifier: GPL-3.0-or-later
"""Language classification for subtitles, embedded and external.

Both embedded streams (tagged via ffprobe's "language" metadata) and sidecar
files (tagged, if at all, by a filename suffix) funnel through `is_english`
so "keep English, discard the rest" means the same thing either way.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

SUBTITLE_EXTENSIONS = frozenset({".srt", ".sub", ".ass", ".ssa", ".vtt", ".idx"})

# ISO 639-1/639-2 English codes plus the spelled-out form some tools write.
_ENGLISH_CODES = {"en", "eng", "english"}

# Qualifiers that ride along after a language in some naming schemes
# ("Movie.eng.sdh.srt") but are not languages themselves.
_NON_LANGUAGE_SUFFIXES = {"forced", "sdh", "cc", "hi"}

_LANG_SUFFIX = re.compile(
    r"\.(?P<tag>[a-z]{2,3}|english|french|spanish|german|italian|portuguese|"
    r"dutch|japanese|chinese|korean|russian|arabic|hindi|swedish|norwegian|"
    r"danish|polish|turkish|greek|czech|hungarian|romanian|finnish|"
    r"forced|sdh|cc|hi)$",
    re.IGNORECASE,
)


def is_english(language: str | None) -> bool:
    """True for an English tag, or no tag at all.

    An untagged track is given the benefit of the doubt: plenty of older
    rips carry exactly one subtitle track and never bothered to label it.
    Only an explicit, recognised non-English tag is treated as foreign.
    """
    if not language:
        return True
    code = language.strip().lower()
    return code in ("", "und", "unknown") or code in _ENGLISH_CODES


@dataclass(frozen=True)
class SidecarSubtitle:
    """An external subtitle file that sits beside a video file."""

    path: Path
    language: str | None
    is_english: bool


def _stem_matches(video_stem: str, candidate_stem: str) -> bool:
    """candidate_stem is video_stem, optionally with a trailing .lang/.tag."""
    if candidate_stem == video_stem:
        return True
    lowered, candidate = video_stem.lower(), candidate_stem.lower()
    return candidate.startswith(lowered + ".") or candidate.startswith(lowered + "_")


def _language_of(stem: str) -> str | None:
    match = _LANG_SUFFIX.search(stem)
    if not match:
        return None
    tag = match.group("tag").lower()
    return None if tag in _NON_LANGUAGE_SUFFIXES else tag


def find_sidecar_subtitles(video_path: str | Path) -> list[SidecarSubtitle]:
    """External subtitle files that belong to this video, by shared stem.

    "Movie.srt" (no language tag) is assumed English. "Movie.en.srt" and
    "Movie.eng.srt" are English. "Movie.fr.srt" and other recognised
    language codes/names are not.
    """
    video_path = Path(video_path)
    stem = video_path.stem
    found: list[SidecarSubtitle] = []
    try:
        entries = sorted(video_path.parent.iterdir())
    except OSError:
        return found
    for entry in entries:
        try:
            if not entry.is_file():
                continue
        except OSError:
            continue
        if entry.suffix.lower() not in SUBTITLE_EXTENSIONS:
            continue
        if not _stem_matches(stem, entry.stem):
            continue
        language = _language_of(entry.stem)
        found.append(SidecarSubtitle(
            path=entry, language=language, is_english=is_english(language),
        ))
    return found
