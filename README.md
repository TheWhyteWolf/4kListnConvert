# vidlib

Scan a video library, group it, and convert 4K files to 1080p — deleting the
original only once the new file has been verified.

CLI with an optional TUI. No dependencies beyond Python 3.11+ and ffmpeg.

## Install

```bash
./bootstrap.sh                          # optional: venv + textual, for the TUI
ln -s "$PWD/bin/vidlib" ~/.local/bin/   # optional: put it on PATH
vidlib doctor --probe-hardware          # check ffmpeg and test GPU encoders
```

## Use

```bash
vidlib scan ~/Videos          # index (cached; rescans are instant)
vidlib groups                 # what have I got, by resolution?
vidlib list --4k
vidlib select --4k --min-size 20G
vidlib convert --selected     # asks for encoder settings, confirms, converts
vidlib tui                    # or do it all visually
```

## Grouping and filtering

`-g/--group`: `resolution` (default), `directory`, `encode`, `codec`, `hdr`,
`container`, `depth`, `audio`, `size`, `efficiency` (bits/pixel — finds bloated
encodes), `fps`, `year`.

Filters work on `list`, `groups`, `dupes`, `select` and `convert`:

```bash
vidlib list --4k --min-size 25G --hdr
vidlib list --codec h264 --4k              # 4K H.264: the biggest wins
vidlib list -g efficiency --min-bpp 0.2    # bloated encodes at any resolution
vidlib list --bloated                      # 4K, or any file that's inefficient at its own res
vidlib groups -g directory --4k            # which folders hold the 4K bulk
vidlib dupes                               # one copy to rule them all
```

`--bloated` is the practical "worth re-encoding" filter: every 4K file (shrinks
by resolution) plus every file at any resolution whose encode is inefficient
for its own size (bits-per-pixel ≥ 0.15 — old codecs, over-bitrate rips).
Everything `--bloated` matches can be shrunk with no visible difference at
1080p.

`dupes` finds titles stored at several resolutions, matching across naming
conventions — `Blade.Runner.2049.2017.2160p…`, `Blade Runner 2049 (2017) 1080p…`
and `Blade Runner 2049 [2017] [2160p]` collapse to one title.

## Converting

```bash
vidlib convert --selected                       # prompts for encoder/CRF/preset
vidlib convert --4k --min-size 30G --dry-run    # plan and savings, changes nothing
vidlib convert --selected -e libx265 -q 20 -p slow -y
```

Each file: encode to a temp file beside the destination → verify it probes
cleanly, has a video stream, is no taller than the target, matches the source
duration within 1%, and is smaller than the source → atomic rename → dispose of
the source. **If any step fails the temp file is removed and the source is left
untouched.**

Don't panic: `--disposal` defaults to `trash` (freedesktop, recoverable via your
file manager). Also `quarantine` (with `--quarantine-dir`), `delete`, `keep`.
Uses `gio trash` when available, else writes `.trashinfo` records directly; files
on another volume use that volume's `.Trash-$UID`, falling back to the home trash
if it isn't writable.

**HDR** — `--tonemap auto` (default) keeps HDR when the encoder can carry it in
10-bit (x265, AV1, NVENC HEVC) and tone-maps to SDR for H.264, where 8-bit HDR
looks washed out. `always` / `never` force it. Tone-mapped H.264 is written
8-bit, since compatibility is the reason to pick H.264.

All audio tracks, attachments, chapters and metadata are copied through. Audio
is copied losslessly by default — on 4K remuxes a TrueHD/Atmos track can
dominate the resulting file, so `--reencode-audio` is often worth it, and
`--stereo` (implies re-encoding — a channel count change can't be a stream
copy) downmixes to 2.0, which costs nothing if you're only ever watching in
stereo. Scaling never upscales and preserves aspect (3840x1600 → 1920x800).
Output names have their resolution/HDR/codec tags rewritten, so
`Movie.2160p.UHD.HDR.x264.mkv` becomes `Movie.1080p.SDR.x265.mkv`.

**Subtitles** default to English only (`sub_language = "eng"`), embedded or
external: an embedded stream with no language tag is kept (plenty of older
rips never bothered to tag the one track they have), an external sidecar is
matched by filename stem — `Movie.srt` and `Movie.en.srt` are both kept as
English, `Movie.fr.srt` is not. A kept external subtitle is muxed into the
output; a discarded one is disposed of the same way as the source (trash by
default) once the conversion succeeds. `--sub-lang LANG` keeps a different
language instead, `--all-subs` disables the filter and keeps everything.

## TUI

`space` select (or whole group on a group row) · `a` toggle group · `A` all 4K ·
`x` clear · `c` convert · `r` rescan · `/` filter · `q` quit.

The group dropdown, filter box and "4K only" toggle apply live. The selection is
shared with the CLI, so you can pick in the TUI and convert from the shell.

## Fast approval queue (`approve/`, Rust)

A second, native TUI purpose-built for quickly approving a batch of
`--bloated` candidates rather than browsing the whole library. It does no
scanning, probing or encoding of its own — it calls `vidlib list --bloated`
for the candidate list, reads their display fields out of vidlib's own
SQLite cache, and on confirmation shells out to `vidlib convert <paths> -y`
for the actual conversion. vidlib stays the only place that decision and
encoding logic lives; this is a UI layer on top of it.

```bash
cd approve && cargo build --release
ln -s "$PWD/target/release/vidlib-approve" ~/.local/bin/   # optional: put it on PATH

vidlib scan /media/movies        # index first, same as the Python CLI
vidlib-approve                   # review every --bloated candidate, biggest first
vidlib-approve --under /media/movies/Live-Action --stereo
```

Candidates default to approved — the point is reviewing a batch quickly, not
re-approving each one. `space` toggles the current row, `a`/`x` approve/clear
everything, `↑`/`↓` (or `j`/`k`) move, `enter` converts whatever is still
approved, `q` quits with nothing converted. `--db`, `--under` and
`--vidlib-bin` point it at a specific library/subdirectory/executable;
anything else on the command line (`--stereo`, `--all-subs`, `--sub-lang`,
`-e`/`-q`/`-p`/`-t`) is forwarded verbatim to `vidlib convert`.

## Other commands

`stats` · `jobs` (history and space reclaimed) · `selection` · `roots --add` ·
`scan --prune` · `config --init`

## Config

`vidlib config --init` writes `~/.config/vidlib/config.toml`:

```toml
roots = ["/media/movies"]
encoder = "libx265"
quality = 20
preset = "medium"
target_height = 1080
tonemap = "auto"
disposal = "trash"
min_size = "50M"      # ignore samples and clips
ask_per_job = true    # prompt for encoder settings on each convert
copy_audio = true
```

Everything is overridable per invocation. State lives in
`~/.local/state/vidlib/library.db` (`--db` to override): metadata cache,
selection, job history.

## Encoders

For a library where the 4K original is deleted afterwards, CPU `libx265` is the
safe default — best quality per byte, and it carries HDR. NVENC is far faster but
less efficient, and HEVC NVENC needs Maxwell GM206 or newer. A driver too old for
your ffmpeg build disables NVENC entirely; `doctor --probe-hardware` test-encodes
a few frames and tells you what actually works, rather than what ffmpeg merely
lists.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

Covers classification, naming, filtering, the database, the trash implementation
and real ffmpeg conversions — including the destructive paths: a rejected,
cancelled or failed encode must all leave the source intact.

## License

GPL-3.0-or-later — see [LICENSE](LICENSE).
