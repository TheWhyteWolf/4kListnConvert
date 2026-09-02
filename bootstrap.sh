#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Create a local virtualenv with the optional TUI dependency.
# The CLI itself needs nothing but Python 3.11+ and ffmpeg.
set -euo pipefail

cd "$(dirname "$0")"

if ! command -v ffmpeg >/dev/null || ! command -v ffprobe >/dev/null; then
    echo "warning: ffmpeg/ffprobe not found on PATH." >&2
    echo "         Arch:   sudo pacman -S ffmpeg" >&2
    echo "         Debian: sudo apt install ffmpeg" >&2
fi

if [ ! -d .venv ]; then
    echo "Creating .venv…"
    python3 -m venv .venv
fi

echo "Installing the TUI dependency (textual)…"
./.venv/bin/pip install --quiet --upgrade pip
./.venv/bin/pip install --quiet textual

echo
echo "Done. Run it with:"
echo "  ./bin/vidlib scan /path/to/videos"
echo "  ./bin/vidlib tui"
echo
echo "Optionally put it on your PATH:"
echo "  ln -s \"$(pwd)/bin/vidlib\" ~/.local/bin/vidlib"
