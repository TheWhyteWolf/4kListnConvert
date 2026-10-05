# SPDX-License-Identifier: GPL-3.0-or-later
"""User configuration, stored as TOML under XDG_CONFIG_HOME."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, asdict, field, fields
from pathlib import Path

from . import trash
from .convert import DEFAULT_ENCODER


def config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "vidlib" / "config.toml"


@dataclass
class Config:
    """Defaults for scanning and converting. Every field is CLI-overridable."""

    roots: list[str] = field(default_factory=list)
    encoder: str = DEFAULT_ENCODER
    quality: int = 20
    preset: str = "medium"
    target_height: int = 1080
    tonemap: str = "auto"           # auto | always | never
    disposal: str = trash.TRASH     # trash | quarantine | delete | keep
    quarantine_dir: str = ""
    copy_audio: bool = True
    audio_codec: str = "aac"
    audio_bitrate: str = "192k"
    audio_channels: int = 0         # 0 keeps the source channel count
    copy_subs: bool = True
    sub_language: str = "eng"       # "eng" filters to English; empty keeps everything
    retag_codec: bool = True
    container: str = ""             # empty means "same as source"
    min_size: str = "50M"           # skip samples and clips
    workers: int = 0                # 0 means auto
    group: str = "resolution"
    include_hidden: bool = False
    follow_symlinks: bool = False
    ask_per_job: bool = True        # prompt for encoder settings on convert

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        target = Path(path) if path else config_path()
        if not target.exists():
            return cls()
        try:
            with open(target, "rb") as handle:
                data = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ValueError(f"cannot read config {target}: {exc}") from exc
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self, path: str | Path | None = None) -> Path:
        target = Path(path) if path else config_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.to_toml(), encoding="utf-8")
        return target

    def to_toml(self) -> str:
        lines = ["# vidlib configuration", ""]
        for key, value in asdict(self).items():
            lines.append(f"{key} = {_toml_value(value)}")
        return "\n".join(lines) + "\n"


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
