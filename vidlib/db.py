# SPDX-License-Identifier: GPL-3.0-or-later
"""SQLite-backed library cache, selection set and conversion job queue.

Probing a large library is slow, so results are cached and only re-probed when
a file's size or mtime changes.
"""

from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path

from .models import VideoFile

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS files (
    path      TEXT PRIMARY KEY,
    size      INTEGER NOT NULL,
    mtime     REAL    NOT NULL,
    probed_at REAL    NOT NULL,
    data      TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS roots (
    path     TEXT PRIMARY KEY,
    added_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS selection (
    path     TEXT PRIMARY KEY,
    added_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    src       TEXT NOT NULL,
    dst       TEXT,
    state     TEXT NOT NULL,
    encoder   TEXT,
    crf       INTEGER,
    preset    TEXT,
    src_size  INTEGER,
    dst_size  INTEGER,
    started   REAL,
    finished  REAL,
    error     TEXT,
    disposal  TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state);
"""


def default_db_path() -> Path:
    state = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return Path(state) / "vidlib" / "library.db"


class Library:
    """Handle on the library database. Usable as a context manager."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=30.0)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(_SCHEMA)
        self.conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema', ?)",
            (str(SCHEMA_VERSION),),
        )
        self.conn.commit()

    def __enter__(self) -> "Library":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        try:
            self.conn.commit()
        finally:
            self.conn.close()

    # --- roots --------------------------------------------------------------

    def add_root(self, path: str | Path) -> None:
        resolved = str(Path(path).expanduser().resolve())
        self.conn.execute(
            "INSERT OR REPLACE INTO roots(path, added_at) VALUES (?, ?)",
            (resolved, time.time()),
        )
        self.conn.commit()

    def roots(self) -> list[str]:
        return [r["path"] for r in self.conn.execute("SELECT path FROM roots ORDER BY path")]

    def remove_root(self, path: str | Path) -> None:
        resolved = str(Path(path).expanduser().resolve())
        self.conn.execute("DELETE FROM roots WHERE path = ?", (resolved,))
        self.conn.commit()

    # --- files --------------------------------------------------------------

    def is_fresh(self, path: str, size: int, mtime: float) -> bool:
        """True when the cached probe still matches what is on disk."""
        row = self.conn.execute(
            "SELECT size, mtime FROM files WHERE path = ?", (path,)
        ).fetchone()
        if row is None:
            return False
        # mtime is a float; filesystems and round-trips make exact equality
        # unreliable, so allow a sub-second tolerance.
        return row["size"] == size and abs(row["mtime"] - mtime) < 1.0

    def put(self, video: VideoFile) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO files(path, size, mtime, probed_at, data) "
            "VALUES (?, ?, ?, ?, ?)",
            (video.path, video.size, video.mtime, video.probed_at, video.to_json()),
        )

    def put_many(self, videos: list[VideoFile]) -> None:
        self.conn.executemany(
            "INSERT OR REPLACE INTO files(path, size, mtime, probed_at, data) "
            "VALUES (?, ?, ?, ?, ?)",
            [(v.path, v.size, v.mtime, v.probed_at, v.to_json()) for v in videos],
        )
        self.conn.commit()

    def get(self, path: str) -> VideoFile | None:
        row = self.conn.execute("SELECT data FROM files WHERE path = ?", (path,)).fetchone()
        return VideoFile.from_json(row["data"]) if row else None

    def all_files(self, under: str | None = None) -> list[VideoFile]:
        if under:
            prefix = str(Path(under).expanduser().resolve())
            rows = self.conn.execute(
                "SELECT data FROM files WHERE path = ? OR path LIKE ? ORDER BY path",
                (prefix, prefix.rstrip("/") + "/%"),
            )
        else:
            rows = self.conn.execute("SELECT data FROM files ORDER BY path")
        return [VideoFile.from_json(r["data"]) for r in rows]

    def iter_paths(self) -> Iterator[str]:
        for row in self.conn.execute("SELECT path FROM files"):
            yield row["path"]

    def forget(self, path: str) -> None:
        self.conn.execute("DELETE FROM files WHERE path = ?", (path,))
        self.conn.execute("DELETE FROM selection WHERE path = ?", (path,))
        self.conn.commit()

    def prune_missing(self, under: str | None = None) -> list[str]:
        """Drop cache rows whose files no longer exist. Returns removed paths."""
        candidates = [v.path for v in self.all_files(under)]
        gone = [p for p in candidates if not os.path.exists(p)]
        if gone:
            self.conn.executemany("DELETE FROM files WHERE path = ?", [(p,) for p in gone])
            self.conn.executemany("DELETE FROM selection WHERE path = ?", [(p,) for p in gone])
            self.conn.commit()
        return gone

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) AS n FROM files").fetchone()["n"]

    # --- selection ----------------------------------------------------------

    def select(self, paths: list[str]) -> int:
        now = time.time()
        cur = self.conn.executemany(
            "INSERT OR IGNORE INTO selection(path, added_at) VALUES (?, ?)",
            [(p, now) for p in paths],
        )
        self.conn.commit()
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else len(paths)

    def deselect(self, paths: list[str]) -> None:
        self.conn.executemany("DELETE FROM selection WHERE path = ?", [(p,) for p in paths])
        self.conn.commit()

    def toggle(self, path: str) -> bool:
        """Flip one path's selected state; returns the new state."""
        if self.is_selected(path):
            self.deselect([path])
            return False
        self.select([path])
        return True

    def is_selected(self, path: str) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM selection WHERE path = ?", (path,)
            ).fetchone()
            is not None
        )

    def selected_paths(self) -> list[str]:
        return [r["path"] for r in self.conn.execute("SELECT path FROM selection ORDER BY path")]

    def selected_files(self) -> list[VideoFile]:
        rows = self.conn.execute(
            "SELECT f.data AS data FROM selection s JOIN files f ON f.path = s.path "
            "ORDER BY s.path"
        )
        return [VideoFile.from_json(r["data"]) for r in rows]

    def clear_selection(self) -> None:
        self.conn.execute("DELETE FROM selection")
        self.conn.commit()

    # --- jobs ---------------------------------------------------------------

    def create_job(self, src: str, dst: str, encoder: str, crf: int, preset: str,
                   src_size: int, disposal: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO jobs(src, dst, state, encoder, crf, preset, src_size, disposal) "
            "VALUES (?, ?, 'pending', ?, ?, ?, ?, ?)",
            (src, dst, encoder, crf, preset, src_size, disposal),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def update_job(self, job_id: int, **fields) -> None:
        if not fields:
            return
        allowed = {"dst", "state", "src_size", "dst_size", "started", "finished", "error", "disposal"}
        cols = {k: v for k, v in fields.items() if k in allowed}
        if not cols:
            return
        assignments = ", ".join(f"{k} = ?" for k in cols)
        self.conn.execute(
            f"UPDATE jobs SET {assignments} WHERE id = ?", (*cols.values(), job_id)
        )
        self.conn.commit()

    def jobs(self, state: str | None = None, limit: int = 200) -> list[sqlite3.Row]:
        if state:
            return list(self.conn.execute(
                "SELECT * FROM jobs WHERE state = ? ORDER BY id DESC LIMIT ?", (state, limit)
            ))
        return list(self.conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)))

    def pending_jobs(self) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM jobs WHERE state IN ('pending', 'running') ORDER BY id"
        ))

    def clear_jobs(self, states: tuple[str, ...] = ("done", "failed", "cancelled")) -> int:
        marks = ",".join("?" * len(states))
        cur = self.conn.execute(f"DELETE FROM jobs WHERE state IN ({marks})", states)
        self.conn.commit()
        return cur.rowcount
