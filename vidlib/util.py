# SPDX-License-Identifier: GPL-3.0-or-later
"""Formatting and small shared helpers. Stdlib only."""

from __future__ import annotations

import os
import re
import shutil
import sys

# --- colour -----------------------------------------------------------------

_COLOR = (
    sys.stdout.isatty()
    and os.environ.get("TERM") not in (None, "dumb")
    and "NO_COLOR" not in os.environ
)

_CODES = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
    "cyan": "\033[36m",
    "grey": "\033[90m",
}


def color(text: str, *styles: str) -> str:
    """Wrap text in ANSI styles, or return it unchanged when not a tty."""
    if not _COLOR or not styles:
        return text
    prefix = "".join(_CODES.get(s, "") for s in styles)
    return f"{prefix}{text}{_CODES['reset']}"


def set_color(enabled: bool) -> None:
    global _COLOR
    _COLOR = enabled


def strip_ansi(text: str) -> str:
    return re.sub(r"\033\[[0-9;]*m", "", text)


def visible_len(text: str) -> int:
    return len(strip_ansi(text))


# --- sizes and durations ----------------------------------------------------

_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")


def human_size(num_bytes: int | float | None) -> str:
    """1234567890 -> '1.15 GB' (binary units, decimal-ish labels)."""
    if num_bytes is None:
        return "-"
    value = float(num_bytes)
    for unit in _UNITS:
        if abs(value) < 1024.0 or unit == _UNITS[-1]:
            if unit == "B":
                return f"{value:.0f} B"
            return f"{value:.2f} {unit}" if abs(value) < 10 else f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} PB"


_SIZE_RE = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*([kmgtp]?)i?b?\s*$", re.I)


def parse_size(text: str) -> int:
    """'10G' / '500mb' / '1.5 TiB' -> bytes. Raises ValueError on junk."""
    match = _SIZE_RE.match(text)
    if not match:
        raise ValueError(f"cannot parse size: {text!r}")
    number, suffix = match.groups()
    power = {"": 0, "k": 1, "m": 2, "g": 3, "t": 4, "p": 5}[suffix.lower()]
    return int(float(number) * (1024**power))


def human_duration(seconds: float | None) -> str:
    """8130.0 -> '2:15:30'."""
    if seconds is None or seconds <= 0:
        return "-"
    total = int(round(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def human_bitrate(bits_per_sec: float | None) -> str:
    if not bits_per_sec or bits_per_sec <= 0:
        return "-"
    mbps = bits_per_sec / 1_000_000
    if mbps >= 10:
        return f"{mbps:.0f} Mb/s"
    if mbps >= 1:
        return f"{mbps:.1f} Mb/s"
    return f"{bits_per_sec / 1000:.0f} kb/s"


# --- tables -----------------------------------------------------------------

def term_width(default: int = 100) -> int:
    try:
        return shutil.get_terminal_size((default, 24)).columns
    except OSError:
        return default


def truncate(text: str, width: int) -> str:
    """Middle-truncate so both the start and the extension stay readable."""
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width <= 3:
        return text[:width]
    keep = width - 1
    head = (keep + 1) // 2
    tail = keep - head
    return f"{text[:head]}…{text[len(text) - tail:]}" if tail else f"{text[:head]}…"


def _fit_widths(widths: list[int], limit: int, flex_column: int, min_width: int = 8) -> None:
    """Shrink column widths in place until the table fits `limit`.

    The nominated flex column gives up space first; if that is not enough the
    remaining width is taken from the widest columns, so one long column cannot
    crush all the others.
    """
    separators = 2 * (len(widths) - 1)

    def overflow() -> int:
        return sum(widths) + separators - limit

    if overflow() > 0 and 0 <= flex_column < len(widths):
        widths[flex_column] -= min(overflow(), max(0, widths[flex_column] - min_width))

    for index in sorted(range(len(widths)), key=lambda i: -widths[i]):
        if overflow() <= 0:
            break
        widths[index] -= min(overflow(), max(0, widths[index] - min_width))


def render_table(
    headers: list[str],
    rows: list[list[str]],
    *,
    align: str = "",
    flex_column: int = 0,
    max_width: int | None = None,
) -> str:
    """Render an aligned table. `align` is one char per column: 'l' or 'r'.

    The column named by `flex_column` absorbs any width overflow so the table
    still fits the terminal.
    """
    if not rows:
        return ""
    ncols = len(headers)
    align = (align + "l" * ncols)[:ncols]
    widths = [visible_len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row[:ncols]):
            widths[i] = max(widths[i], visible_len(cell))

    limit = max_width if max_width is not None else term_width()
    _fit_widths(widths, limit, flex_column)

    def fmt(cell: str, width: int, how: str) -> str:
        shown = visible_len(cell)
        if shown > width:
            # Truncation has to happen on the visible text, so drop styling.
            cell = truncate(strip_ansi(cell), width)
            shown = visible_len(cell)
        pad = " " * (width - shown)
        return pad + cell if how == "r" else cell + pad

    lines = [
        "  ".join(
            color(fmt(h, w, a), "bold") for h, w, a in zip(headers, widths, align)
        ).rstrip(),
        color("  ".join("─" * w for w in widths), "grey"),
    ]
    for row in rows:
        cells = list(row[:ncols]) + [""] * (ncols - len(row))
        lines.append("  ".join(fmt(c, w, a) for c, w, a in zip(cells, widths, align)).rstrip())
    return "\n".join(lines)


def plural(count: int, singular: str, suffix: str = "s") -> str:
    return f"{count} {singular}{'' if count == 1 else suffix}"
