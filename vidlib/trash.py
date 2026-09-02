"""Disposal of source files after a successful conversion.

Three modes: freedesktop trash (default, recoverable), a quarantine directory,
or permanent unlink. The trash implementation prefers `gio trash` and falls
back to writing the freedesktop trashinfo records directly.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

TRASH = "trash"
QUARANTINE = "quarantine"
DELETE = "delete"
KEEP = "keep"
DISPOSAL_MODES = (TRASH, QUARANTINE, DELETE, KEEP)


class DisposalError(RuntimeError):
    pass


def _nearest_existing(path: Path) -> Path:
    """Closest ancestor of path that exists, so a not-yet-created directory
    can still be attributed to the right filesystem."""
    current = path if path.is_absolute() else path.resolve()
    while not current.exists() and current != current.parent:
        current = current.parent
    return current


def _device_of(path: Path) -> int | None:
    try:
        return _nearest_existing(path).stat().st_dev
    except OSError:
        return None


def _mount_point(path: Path) -> Path:
    """The mount point containing path, found by walking up device boundaries."""
    current = _nearest_existing(Path(path).resolve())
    if current.is_file():
        current = current.parent
    try:
        dev = current.stat().st_dev
    except OSError:
        return Path("/")
    while current != current.parent:
        parent = current.parent
        try:
            if parent.stat().st_dev != dev:
                return current
        except OSError:
            return current
        current = parent
    return current


def _home_trash() -> Path:
    data_home = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(data_home) / "Trash"


def _unique_name(directory: Path, name: str) -> str:
    """Pick a filename free in both files/ and info/ of a trash dir."""
    candidate = name
    stem, ext = os.path.splitext(name)
    counter = 1
    while (directory / "files" / candidate).exists() or (
        directory / "info" / f"{candidate}.trashinfo"
    ).exists():
        candidate = f"{stem}.{counter}{ext}"
        counter += 1
    return candidate


def _candidate_trash_dirs(path: Path) -> list[Path]:
    """Trash directories to try for path, best first.

    Same-volume trash avoids copying tens of gigabytes across filesystems, but
    a media volume is often read-only for the trash directory or owned by
    another user, so the home trash is always kept as a fallback.
    """
    home_trash = _home_trash()
    home_dev = _device_of(home_trash)
    file_dev = _device_of(path.parent)
    if home_dev is not None and home_dev == file_dev:
        return [home_trash]
    # Spec also allows an admin-created $top/.Trash with the sticky bit; the
    # per-uid directory is the portable option.
    volume_trash = _mount_point(path) / f".Trash-{os.getuid()}"
    return [volume_trash, home_trash]


def _trash_dir_for(path: Path) -> Path:
    """The trash directory that will be used for path."""
    return _candidate_trash_dirs(path)[0]


def _freedesktop_trash(path: Path) -> Path:
    """Move path into a trash directory, writing its .trashinfo record."""
    problems: list[str] = []
    for trash_dir in _candidate_trash_dirs(path):
        try:
            return _trash_into(trash_dir, path)
        except DisposalError as exc:
            problems.append(str(exc))
    raise DisposalError("; ".join(problems) or f"no usable trash directory for {path}")


def _trash_into(trash_dir: Path, path: Path) -> Path:
    """Move path into one specific trash directory."""
    files_dir = trash_dir / "files"
    info_dir = trash_dir / "info"
    try:
        files_dir.mkdir(parents=True, exist_ok=True)
        info_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DisposalError(f"cannot create trash directory {trash_dir}: {exc}") from exc

    name = _unique_name(trash_dir, path.name)
    target = files_dir / name
    info_path = info_dir / f"{name}.trashinfo"

    # Write the record first: an orphaned info file is harmless, whereas a
    # trashed file with no record is unrestorable.
    deletion_date = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    info_path.write_text(
        "[Trash Info]\n"
        f"Path={quote(str(path.resolve()), safe='/')}\n"
        f"DeletionDate={deletion_date}\n",
        encoding="utf-8",
    )
    try:
        os.rename(path, target)
    except OSError:
        # Different filesystem, or rename refused: fall back to copy+unlink.
        try:
            shutil.move(str(path), str(target))
        except OSError as exc:
            info_path.unlink(missing_ok=True)
            raise DisposalError(f"cannot move {path} to trash: {exc}") from exc
    return target


def _gio_trash(path: Path) -> bool:
    """Trash via gio, which handles cross-volume and desktop integration."""
    gio = shutil.which("gio")
    if not gio:
        return False
    try:
        result = subprocess.run(
            [gio, "trash", "--", str(path)],
            capture_output=True, text=True, timeout=60, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def send_to_trash(path: str | Path, *, prefer_gio: bool = True) -> str:
    """Move a file to the trash. Returns a human description of where it went."""
    path = Path(path)
    if not path.exists():
        raise DisposalError(f"{path} does not exist")
    if prefer_gio and _gio_trash(path):
        return "trash"
    target = _freedesktop_trash(path)
    return str(target)


def quarantine(path: str | Path, quarantine_dir: str | Path) -> str:
    """Move a file into a holding directory for later review."""
    path = Path(path)
    directory = Path(quarantine_dir).expanduser()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / path.name
    counter = 1
    stem, ext = os.path.splitext(path.name)
    while target.exists():
        target = directory / f"{stem}.{counter}{ext}"
        counter += 1
    try:
        shutil.move(str(path), str(target))
    except OSError as exc:
        raise DisposalError(f"cannot quarantine {path}: {exc}") from exc
    return str(target)


def dispose(
    path: str | Path,
    mode: str = TRASH,
    *,
    quarantine_dir: str | Path | None = None,
) -> str:
    """Apply the configured disposal to a source file.

    Returns a short description of what happened, for logging and reports.
    """
    path = Path(path)
    if mode == KEEP:
        return "kept"
    if not path.exists():
        raise DisposalError(f"{path} does not exist")
    if mode == TRASH:
        where = send_to_trash(path)
        # The full trashed path is long and the name may have been uniquified,
        # so name the recoverable location rather than the exact file.
        if where == "trash":
            return "moved to trash"
        return f"moved to trash ({Path(where).parent.parent})"
    if mode == QUARANTINE:
        if not quarantine_dir:
            raise DisposalError("quarantine mode requires a quarantine directory")
        return f"quarantined in {Path(quarantine(path, quarantine_dir)).parent}"
    if mode == DELETE:
        try:
            path.unlink()
        except OSError as exc:
            raise DisposalError(f"cannot delete {path}: {exc}") from exc
        return "deleted permanently"
    raise DisposalError(f"unknown disposal mode: {mode!r}")
