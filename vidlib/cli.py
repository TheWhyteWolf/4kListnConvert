"""Command-line interface for vidlib."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import signal
import sys
import time
from pathlib import Path

from . import trash
from .config import Config, config_path
from .convert import (
    ENCODERS, DEFAULT_ENCODER, ConversionPlan, plan_conversion, run_conversion,
)
from .db import Library, default_db_path
from .grouping import (
    Filter, GROUPERS, display_group_name, find_duplicates, group_files, summarize,
)
from .models import RESOLUTION_ORDER, VideoFile
from .probe import FFmpegMissing, available_encoders, ffmpeg_bin
from .scanner import scan
from .util import (
    color, human_bitrate, human_duration, human_size, parse_size, plural,
    render_table, term_width, truncate,
)


def _fit(line: str, width: int) -> str:
    """A carriage-returned line padded to the full width, so it overwrites
    whatever longer line was printed before it."""
    return "\r" + line[: width - 1].ljust(width - 1)


def warn(message: str) -> None:
    print(color(f"warning: {message}", "yellow"), file=sys.stderr)


def fail(message: str, code: int = 1) -> "int":
    print(color(f"error: {message}", "red"), file=sys.stderr)
    return code


# --- shared arguments -------------------------------------------------------


def add_filter_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("filters")
    group.add_argument("--4k", dest="only_4k", action="store_true",
                       help="only 2160p/4320p files")
    group.add_argument("-r", "--resolution", action="append", default=[],
                       metavar="RES", help="e.g. 2160p (repeatable)")
    group.add_argument("-c", "--codec", action="append", default=[],
                       metavar="CODEC", help="e.g. hevc, h264 (repeatable)")
    group.add_argument("--container", action="append", default=[], metavar="EXT",
                       help="e.g. mkv, mp4 (repeatable)")
    group.add_argument("--hdr", action="store_true", help="only HDR/HLG/DV files")
    group.add_argument("--sdr", action="store_true", help="only SDR files")
    group.add_argument("--min-size", metavar="SIZE", help="e.g. 10G")
    group.add_argument("--max-size", metavar="SIZE", help="e.g. 60G")
    group.add_argument("--min-bitrate", metavar="MBPS", type=float,
                       help="minimum overall bitrate in Mb/s")
    group.add_argument("--min-bpp", metavar="BPP", type=float,
                       help="minimum bits per pixel (finds bloated encodes)")
    group.add_argument("-m", "--match", metavar="TEXT",
                       help="substring or glob against the full path")
    group.add_argument("--under", metavar="DIR", help="restrict to a subdirectory")
    group.add_argument("--errors", action="store_true",
                       help="include files that could not be probed")


def build_filter(args) -> Filter:
    def size(value):
        return parse_size(value) if value else 0

    return Filter(
        resolutions=set(args.resolution or []),
        codecs={c.lower() for c in (args.codec or [])},
        containers={c.lower().lstrip(".") for c in (args.container or [])},
        only_4k=args.only_4k,
        only_hdr=args.hdr,
        exclude_hdr=args.sdr,
        min_size=size(args.min_size),
        max_size=size(args.max_size),
        min_bitrate=(args.min_bitrate or 0) * 1_000_000,
        min_bpp=args.min_bpp or 0.0,
        pattern=args.match or "",
        under=args.under or "",
        include_errors=args.errors,
    )


def open_library(args) -> Library:
    return Library(args.db)


def resolve_roots(args, config: Config, library: Library) -> list[str]:
    if getattr(args, "paths", None):
        return [str(Path(p).expanduser().resolve()) for p in args.paths]
    stored = library.roots()
    if stored:
        return stored
    if config.roots:
        return [str(Path(p).expanduser().resolve()) for p in config.roots]
    return []


def select_files(args, library: Library) -> list[VideoFile]:
    """Apply filters to the cached library, honouring --selected."""
    if getattr(args, "selected", False):
        files = library.selected_files()
    else:
        files = library.all_files()
    return build_filter(args).apply(files)


# --- commands ---------------------------------------------------------------


def cmd_scan(args, config: Config) -> int:
    with open_library(args) as library:
        roots = resolve_roots(args, config, library)
        if not roots:
            return fail("no directory given and no roots configured. "
                        "Try: vidlib scan /path/to/videos")
        missing = [r for r in roots if not os.path.isdir(r)]
        for root in missing:
            warn(f"not a directory, skipping: {root}")
        roots = [r for r in roots if r not in missing]
        if not roots:
            return fail("no valid directories to scan")

        for root in roots:
            library.add_root(root)

        min_size = parse_size(args.min_size) if args.min_size else parse_size(config.min_size)
        width = term_width()
        last = [0.0]

        show_progress = sys.stderr.isatty()

        def progress(done: int, total: int, path: str) -> None:
            if not show_progress:
                return
            now = time.time()
            if now - last[0] < 0.08 and done < total:
                return
            last[0] = now
            bar_width = max(10, min(30, width - 50))
            filled = int(bar_width * done / total) if total else bar_width
            bar = "█" * filled + "░" * (bar_width - filled)
            counter = f"{done}/{total}"
            label = truncate(os.path.basename(path), max(10, width - bar_width - len(counter) - 6))
            print(_fit(f"  {bar} {counter}  {label}", width), end="", flush=True)

        noun = "directory" if len(roots) == 1 else "directories"
        print(f"Scanning {len(roots)} {noun}: {', '.join(roots)}")
        try:
            stats = scan(
                roots, library,
                workers=args.workers or config.workers,
                force=args.force,
                min_size=min_size,
                include_hidden=config.include_hidden,
                follow_symlinks=config.follow_symlinks,
                progress=progress,
            )
        except FFmpegMissing as exc:
            print()
            return fail(str(exc))
        if show_progress:
            print("\r" + " " * width + "\r", end="")
        print(color(f"Scan complete: {stats}", "green"))

        if args.prune:
            gone = library.prune_missing()
            if gone:
                print(f"Pruned {plural(len(gone), 'missing file')} from the cache.")

        files = [v for v in library.all_files() if not v.probe_error]
        info = summarize(files)
        print(f"Library: {plural(info['count'], 'file')}, {human_size(info['size'])} total, "
              f"{info['count_4k']} in 4K ({human_size(info['size_4k'])}).")
    return 0


def _resolution_flag(video: VideoFile) -> str:
    if video.is_4k:
        return color("4K", "cyan", "bold")
    return video.resolution


def cmd_list(args, config: Config) -> int:
    with open_library(args) as library:
        files = select_files(args, library)
        if not files:
            print("No files match. Run 'vidlib scan <dir>' first, or relax the filters.")
            return 0

        if args.paths_only:
            for video in files:
                print(video.path)
            return 0

        selected = set(library.selected_paths())
        key = args.group or config.group
        roots = library.roots()
        total_shown = 0
        for name, group in group_files(files, key):
            if args.limit and total_shown >= args.limit:
                break
            size = sum(v.size for v in group)
            header = display_group_name(key, name, roots)
            print()
            print(color(f"{header}", "bold", "blue"),
                  color(f"({plural(len(group), 'file')}, {human_size(size)})", "grey"))
            rows = []
            for video in group:
                if args.limit and total_shown >= args.limit:
                    break
                total_shown += 1
                mark = color("●", "green") if video.path in selected else " "
                rows.append([
                    mark,
                    video.name,
                    _resolution_flag(video),
                    video.encode,
                    video.hdr if video.is_hdr else "",
                    human_size(video.size),
                    human_duration(video.duration),
                    human_bitrate(video.overall_bitrate),
                ])
            print(render_table(
                ["", "Name", "Res", "Encode", "HDR", "Size", "Length", "Bitrate"],
                rows, align="lllllrrr", flex_column=1,
            ))

        info = summarize(files)
        print()
        print(color(
            f"{plural(info['count'], 'file')}  ·  {human_size(info['size'])}  ·  "
            f"{info['count_4k']} in 4K ({human_size(info['size_4k'])})", "bold"))
        if info["errors"]:
            warn(f"{info['errors']} file(s) could not be probed; see 'vidlib list --errors'")
    return 0


def cmd_groups(args, config: Config) -> int:
    with open_library(args) as library:
        files = select_files(args, library)
        if not files:
            print("No files match.")
            return 0
        key = args.group or config.group
        roots = library.roots()
        rows = []
        for name, group in group_files(files, key):
            size = sum(v.size for v in group)
            fourk = [v for v in group if v.is_4k]
            rows.append([
                display_group_name(key, name, roots),
                str(len(group)),
                human_size(size),
                str(len(fourk)) if fourk else "",
                human_size(sum(v.size for v in fourk)) if fourk else "",
                human_duration(sum(v.duration or 0 for v in group)),
            ])
        label = GROUPERS[key][0]
        print(color(f"Grouped by {label.lower()}", "bold"))
        print()
        print(render_table(
            [label, "Files", "Size", "4K", "4K size", "Length"],
            rows, align="lrrrrr", flex_column=0,
        ))
        info = summarize(files)
        print()
        print(color(f"Total: {plural(info['count'], 'file')}, {human_size(info['size'])}", "bold"))
    return 0


def cmd_dupes(args, config: Config) -> int:
    with open_library(args) as library:
        files = select_files(args, library)
        dupes = find_duplicates(files, require_distinct_resolution=not args.same_resolution)
        if not dupes:
            print("No duplicate titles found.")
            return 0
        reclaimable = 0
        for dupe in dupes:
            print()
            print(color(dupe.title or "(untitled)", "bold", "blue"),
                  color(f"— {' / '.join(dupe.resolutions)}", "grey"))
            rows = []
            for index, video in enumerate(dupe.files):
                keep = color("keep", "green") if index == 0 else color("extra", "yellow")
                rows.append([keep, video.name, video.resolution, video.encode,
                             human_size(video.size), video.directory])
            print(render_table(["", "Name", "Res", "Encode", "Size", "Directory"],
                               rows, align="llllrl", flex_column=5))
            reclaimable += dupe.reclaimable
        print()
        print(color(f"{plural(len(dupes), 'duplicate title')}; "
                    f"{human_size(reclaimable)} reclaimable by keeping the best copy.", "bold"))
    return 0


def cmd_select(args, config: Config) -> int:
    with open_library(args) as library:
        if args.clear:
            library.clear_selection()
            print("Selection cleared.")
            return 0
        files = select_files(args, library)
        if args.paths:
            wanted = {str(Path(p).expanduser().resolve()) for p in args.paths}
            files = [v for v in library.all_files() if v.path in wanted]
        if not files:
            print("Nothing matched; selection unchanged.")
            return 0
        paths = [v.path for v in files]
        if args.remove:
            library.deselect(paths)
            print(f"Deselected {plural(len(paths), 'file')}.")
        else:
            library.select(paths)
            total = sum(v.size for v in files)
            print(f"Selected {plural(len(paths), 'file')} ({human_size(total)}).")
        current = library.selected_files()
        print(f"Selection now holds {plural(len(current), 'file')} "
              f"({human_size(sum(v.size for v in current))}).")
    return 0


def cmd_selection(args, config: Config) -> int:
    with open_library(args) as library:
        files = library.selected_files()
        if not files:
            print("Selection is empty.")
            return 0
        rows = [[v.name, v.resolution, v.encode, human_size(v.size),
                 human_size(v.estimated_saving(config.encoder, config.target_height))]
                for v in files]
        print(render_table(["Name", "Res", "Encode", "Size", "Est. saving"],
                           rows, align="lllrr", flex_column=0))
        total = sum(v.size for v in files)
        saving = sum(v.estimated_saving(config.encoder, config.target_height) for v in files)
        print()
        print(color(f"{plural(len(files), 'file')}, {human_size(total)}; "
                    f"estimated saving {human_size(saving)}.", "bold"))
    return 0


def cmd_stats(args, config: Config) -> int:
    with open_library(args) as library:
        files = [v for v in library.all_files() if not v.probe_error]
        if not files:
            print("Library is empty. Run 'vidlib scan <dir>'.")
            return 0
        info = summarize(files)
        print(color("Library", "bold"))
        print(f"  Files          {info['count']}")
        print(f"  Total size     {human_size(info['size'])}")
        print(f"  Total runtime  {human_duration(info['duration'])}")
        print(f"  Roots          {', '.join(library.roots()) or '(none)'}")
        print(f"  Database       {library.path}")
        print()
        print(color("4K candidates", "bold"))
        fourk = [v for v in files if v.is_4k]
        saving = sum(v.estimated_saving(config.encoder, config.target_height) for v in fourk)
        print(f"  Files          {len(fourk)}")
        print(f"  Size           {human_size(info['size_4k'])}")
        print(f"  Est. reclaim   {human_size(saving)} "
              f"(converting all to {config.target_height}p with {config.encoder})")
        print()
        for key in ("resolution", "encode", "hdr", "container"):
            groups = group_files(files, key)
            summary = "  ".join(f"{name} {len(g)}" for name, g in groups[:8])
            print(f"  {GROUPERS[key][0]:<14} {summary}")
        errors = [v for v in library.all_files() if v.probe_error]
        if errors:
            print()
            warn(f"{len(errors)} unreadable file(s)")
    return 0


def cmd_doctor(args, config: Config) -> int:
    print(color("Toolchain", "bold"))
    ok = True
    try:
        print(f"  ffmpeg   {ffmpeg_bin()}")
    except FFmpegMissing as exc:
        print(color(f"  ffmpeg   MISSING — {exc}", "red"))
        ok = False
    print(f"  gio      {shutil.which('gio') or color('not found (using built-in trash)', 'yellow')}")
    print(f"  database {args.db or default_db_path()}")
    print(f"  config   {config_path()}{'' if config_path().exists() else ' (not created yet)'}")

    if ok:
        print()
        print(color("Encoders", "bold"))
        present = available_encoders()
        width = term_width()
        for name, spec in ENCODERS.items():
            if name not in present:
                status, detail = color("not built in", "grey"), ""
            elif spec.hardware and args.probe_hardware:
                status, detail = _test_encoder(name)
            else:
                status, detail = color("available", "green"), ""
            # Pad before colouring: ANSI codes would otherwise count as width.
            print(f"  {color(name.ljust(14), 'cyan')}  {spec.label:<22} {status}")
            print(color(f"      {truncate(spec.notes, max(20, width - 8))}", "grey"))
            if detail:
                print(color(f"      {truncate(detail, max(20, width - 8))}", "red"))
        if not args.probe_hardware:
            print()
            print(color("  Hardware encoders are listed if ffmpeg was built with them; that does "
                        "not mean your GPU supports them.", "grey"))
            print(color("  Run 'vidlib doctor --probe-hardware' to actually test each one.", "grey"))
    return 0 if ok else 1


def _test_encoder(name: str) -> tuple[str, str]:
    """Encode a couple of frames to see whether the GPU really supports it."""
    import subprocess
    cmd = [
        ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=24:duration=0.2",
        "-c:v", name, "-f", "null", "-",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return color("test failed", "red"), str(exc)
    if result.returncode == 0:
        return color("works", "green"), ""
    return color("unsupported", "red"), _root_cause(result.stderr, name)


def _root_cause(stderr: str | None, encoder: str) -> str:
    """Pick the line that explains the failure.

    ffmpeg's last line is usually the downstream symptom ("Nothing was written
    into output file"); the real reason is the first line the encoder logged.
    """
    lines = [ln.strip() for ln in (stderr or "").splitlines() if ln.strip()]
    if not lines:
        return "unknown error"
    noise = ("nothing was written", "task finished", "terminating thread",
             "error sending frames", "could not open encoder before eof")
    for line in lines:
        low = line.lower()
        if any(marker in low for marker in noise):
            continue
        # Strip ffmpeg's "[h264_nvenc @ 0x...] " prefix.
        cleaned = re.sub(r"^\[[^\]]+\]\s*", "", line)
        if cleaned:
            return cleaned[:90]
    return lines[0][:90]


def cmd_config(args, config: Config) -> int:
    path = config_path()
    if args.init:
        if path.exists() and not args.force:
            return fail(f"{path} already exists (use --force to overwrite)")
        written = config.save()
        print(f"Wrote {written}")
        return 0
    if path.exists():
        print(f"# {path}")
        print(path.read_text(encoding="utf-8"))
    else:
        print(f"# no config file yet at {path}")
        print("# defaults shown; run 'vidlib config --init' to write them")
        print()
        print(config.to_toml())
    return 0


def cmd_roots(args, config: Config) -> int:
    with open_library(args) as library:
        if args.add:
            for path in args.add:
                resolved = Path(path).expanduser().resolve()
                if not resolved.is_dir():
                    warn(f"not a directory: {resolved}")
                    continue
                library.add_root(resolved)
                print(f"Added root {resolved}")
        if args.remove:
            for path in args.remove:
                library.remove_root(path)
                print(f"Removed root {Path(path).expanduser().resolve()}")
        roots = library.roots()
        if not args.add and not args.remove:
            if not roots:
                print("No roots configured. Add one with 'vidlib roots --add /path'.")
            for root in roots:
                marker = "" if os.path.isdir(root) else color("  (missing)", "red")
                print(f"  {root}{marker}")
    return 0


def cmd_jobs(args, config: Config) -> int:
    with open_library(args) as library:
        if args.clear:
            removed = library.clear_jobs()
            print(f"Cleared {plural(removed, 'finished job')}.")
            return 0
        jobs = library.jobs(state=args.state, limit=args.limit)
        if not jobs:
            print("No conversion jobs recorded yet.")
            return 0
        rows = []
        for job in jobs:
            saved = ""
            if job["src_size"] and job["dst_size"]:
                saved = human_size(job["src_size"] - job["dst_size"])
            elapsed = ""
            if job["started"] and job["finished"]:
                elapsed = human_duration(job["finished"] - job["started"])
            state = job["state"]
            tint = {"done": "green", "failed": "red", "running": "cyan"}.get(state, "grey")
            rows.append([
                str(job["id"]), color(state, tint), os.path.basename(job["src"]),
                job["encoder"] or "", str(job["crf"] or ""),
                human_size(job["src_size"]) if job["src_size"] else "",
                human_size(job["dst_size"]) if job["dst_size"] else "",
                saved, elapsed, (job["error"] or "")[:40],
            ])
        print(render_table(
            ["ID", "State", "File", "Encoder", "Q", "From", "To", "Saved", "Time", "Error"],
            rows, align="lllllrrrrl", flex_column=2,
        ))
        done = [j for j in jobs if j["state"] == "done" and j["src_size"] and j["dst_size"]]
        if done:
            total = sum(j["src_size"] - j["dst_size"] for j in done)
            print()
            print(color(f"Reclaimed {human_size(total)} across "
                        f"{plural(len(done), 'conversion')}.", "bold"))
    return 0


def cmd_tui(args, config: Config) -> int:
    try:
        from .tui.app import run_tui
    except ImportError as exc:
        return fail(
            f"the TUI needs Textual ({exc}).\n"
            "  Install it with:  ./bootstrap.sh     (creates .venv)\n"
            "  or:               pip install --user textual"
        )
    return run_tui(db_path=args.db, config=config)


# --- convert ----------------------------------------------------------------


def _prompt(question: str, default: str, options: list[str] | None = None) -> str:
    suffix = f" [{default}]"
    while True:
        try:
            answer = input(color(f"  {question}{suffix}: ", "cyan")).strip()
        except EOFError:
            return default
        if not answer:
            return default
        if options and answer not in options:
            print(color(f"    choose one of: {', '.join(options)}", "yellow"))
            continue
        return answer


def _ask_settings(config: Config, present: set[str]) -> tuple[str, int, str]:
    """Interactive per-batch encoder settings, as configured by ask_per_job."""
    usable = [name for name in ENCODERS if name in present] or [DEFAULT_ENCODER]
    print()
    print(color("Encoder settings for this batch", "bold"))
    for name in usable:
        spec = ENCODERS[name]
        marker = color(" (default)", "grey") if name == config.encoder else ""
        print(f"    {color(name, 'cyan'):<24} {spec.label}{marker}")
        print(color(f"      {spec.notes}", "grey"))
    encoder = _prompt("encoder", config.encoder if config.encoder in usable else usable[0], usable)
    spec = ENCODERS[encoder]
    quality_name = "CRF" if spec.quality_flag == "-crf" else "CQ"
    while True:
        raw = _prompt(f"{quality_name} (lower = better quality)", str(spec.default_quality))
        try:
            quality = int(raw)
            break
        except ValueError:
            print(color("    enter a whole number", "yellow"))
    preset = _prompt("preset", spec.default_preset, list(spec.presets))
    return encoder, quality, preset


def _render_progress(prefix: str, percent: float, speed: float, eta: float | None) -> None:
    if not sys.stdout.isatty():
        return
    width = term_width()
    bar_width = max(10, min(28, width - len(prefix) - 30))
    filled = int(bar_width * percent / 100)
    bar = "█" * filled + "░" * (bar_width - filled)
    eta_text = f"ETA {human_duration(eta)}" if eta else "ETA --"
    line = f"  {prefix} {bar} {percent:5.1f}%  {speed:4.1f}x  {eta_text}"
    print(_fit(line, width), end="", flush=True)


def cmd_convert(args, config: Config) -> int:
    with open_library(args) as library:
        if args.paths:
            wanted = {str(Path(p).expanduser().resolve()) for p in args.paths}
            files = [v for v in library.all_files() if v.path in wanted]
            unknown = wanted - {v.path for v in files}
            for path in sorted(unknown):
                warn(f"not in the library (scan it first): {path}")
        elif args.selected or (not _has_filters(args) and library.selected_paths()):
            files = build_filter(args).apply(library.selected_files())
            if not args.selected:
                print(color("Using the saved selection.", "grey"))
        else:
            files = select_files(args, library)

        files = [v for v in files if not v.probe_error]
        if not files:
            return fail("nothing to convert. Select files first, or pass filters "
                        "such as --4k. See 'vidlib list --4k'.")

        already = [v for v in files if (v.height or 0) <= args.target_height and not args.force]
        if already:
            warn(f"skipping {plural(len(already), 'file')} already at or below "
                 f"{args.target_height}p (use --force to convert anyway)")
            files = [v for v in files if v not in already]
        if not files:
            return fail("every matching file is already at or below the target height")

        present = available_encoders()
        if args.encoder or args.quality is not None or args.preset or args.yes or not sys.stdin.isatty():
            encoder = args.encoder or config.encoder
            spec = ENCODERS.get(encoder)
            if spec is None:
                return fail(f"unknown encoder {encoder!r}")
            quality = args.quality if args.quality is not None else spec.default_quality
            preset = args.preset or spec.default_preset
        elif config.ask_per_job:
            encoder, quality, preset = _ask_settings(config, present)
        else:
            encoder = config.encoder
            quality = config.quality
            preset = config.preset

        if encoder not in ENCODERS:
            return fail(f"unknown encoder {encoder!r}")
        if encoder not in present:
            warn(f"this ffmpeg build does not list {encoder}; the encode will likely fail")
        spec = ENCODERS[encoder]
        if preset not in spec.presets:
            return fail(f"preset {preset!r} is not valid for {encoder} "
                        f"(choose from {', '.join(spec.presets)})")

        disposal = args.disposal or config.disposal
        quarantine_dir = args.quarantine_dir or config.quarantine_dir or None
        if disposal == trash.QUARANTINE and not quarantine_dir:
            return fail("--disposal quarantine needs --quarantine-dir (or set it in the config)")

        plans: list[ConversionPlan] = []
        for video in files:
            plans.append(plan_conversion(
                video, encoder=encoder, quality=quality, preset=preset,
                target_height=args.target_height, tonemap=args.tonemap or config.tonemap,
                copy_audio=not args.reencode_audio,
                audio_codec=config.audio_codec, audio_bitrate=config.audio_bitrate,
                copy_subs=config.copy_subs, disposal=disposal,
                quarantine_dir=quarantine_dir,
                container=args.container_out or config.container or None,
                retag_codec=config.retag_codec,
            ))

        _print_plan_table(plans, encoder, quality, preset, disposal, args.target_height)

        if args.dry_run:
            print(color("\nDry run: nothing was changed.", "yellow"))
            return 0

        if not args.yes:
            if not sys.stdin.isatty():
                return fail("refusing to convert without confirmation; pass --yes")
            verb = (
                color("PERMANENTLY DELETED", "red", "bold") if disposal == trash.DELETE
                else f"moved to {quarantine_dir}" if disposal == trash.QUARANTINE
                else DISPOSAL_PHRASE[disposal]
            )
            print()
            print(f"After each successful conversion the source will be {verb}.")
            answer = input(color(f"Convert {plural(len(plans), 'file')}? [y/N] ", "bold")).strip().lower()
            if answer not in ("y", "yes"):
                print("Aborted.")
                return 1

        return _run_batch(plans, library, args)


DISPOSAL_PHRASE = {
    trash.TRASH: "moved to the trash",
    trash.DELETE: "permanently deleted",
    trash.QUARANTINE: "moved to the quarantine directory",
    trash.KEEP: "left in place",
}


def _has_filters(args) -> bool:
    return bool(
        args.only_4k or args.resolution or args.codec or args.container or args.hdr
        or args.sdr or args.min_size or args.max_size or args.min_bitrate
        or args.min_bpp or args.match or args.under
    )


def _print_plan_table(plans, encoder, quality, preset, disposal, target_height) -> None:
    rows = []
    for plan in plans:
        estimate = plan.estimated_output
        note = []
        if plan.tonemap:
            note.append("tonemap→SDR")
        if plan.preserve_hdr:
            note.append("keep HDR")
        rows.append([
            plan.source.name,
            plan.source.dimensions,
            human_size(plan.source.size),
            f"~{human_size(estimate)}" if estimate else "?",
            f"~{human_size(plan.source.size - estimate)}" if estimate else "?",
            ", ".join(note),
        ])
    print()
    fate = DISPOSAL_PHRASE.get(disposal, disposal)
    print(color(f"Converting to {target_height}p with {encoder} "
                f"{ENCODERS[encoder].quality_flag.lstrip('-').upper()} {quality}, "
                f"preset {preset}", "bold"))
    print(color(f"Sources will be {fate} once the new file is verified.", "bold"))
    print()
    print(render_table(["File", "Source", "Size", "Est. out", "Est. saved", "Notes"],
                       rows, align="llrrrl", flex_column=0))
    total = sum(p.source.size for p in plans)
    estimated = sum(p.estimated_output or p.source.size for p in plans)
    print()
    print(color(f"{plural(len(plans), 'file')}: {human_size(total)} → "
                f"~{human_size(estimated)}  (est. saving {human_size(total - estimated)})", "bold"))
    print(color("Size estimates are rough; actual results depend on the content.", "grey"))


def _run_batch(plans: list[ConversionPlan], library: Library, args) -> int:
    cancelled = {"flag": False}

    def on_sigint(signum, frame):
        if cancelled["flag"]:
            raise KeyboardInterrupt
        cancelled["flag"] = True
        print(color("\n  Cancelling after the current file… (Ctrl-C again to abort now)", "yellow"))

    previous = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, on_sigint)

    succeeded = failed = skipped = 0
    reclaimed = 0
    started_all = time.monotonic()
    try:
        for index, plan in enumerate(plans, start=1):
            if cancelled["flag"]:
                skipped += 1
                continue
            prefix = f"[{index}/{len(plans)}]"
            name = truncate(plan.source.name, 40)
            print(f"\n{color(prefix, 'bold')} {name}")
            print(color(f"  {plan.source.dimensions} → target {plan.target_height}p  "
                        f"({human_size(plan.source.size)})", "grey"))

            job_id = library.create_job(
                plan.source.path, str(plan.destination), plan.encoder,
                plan.quality, plan.preset, plan.source.size, plan.disposal,
            )
            library.update_job(job_id, state="running", started=time.time())

            def progress(update, _prefix=prefix):
                _render_progress(_prefix, update.percent, update.speed, update.eta_seconds)

            # Deliberately no should_cancel: an interrupt stops the queue but
            # lets the file in flight finish, rather than wasting the encode.
            result = run_conversion(plan, progress=progress)
            if sys.stdout.isatty():
                print("\r" + " " * term_width() + "\r", end="")

            if result.ok:
                succeeded += 1
                reclaimed += result.saved
                library.update_job(job_id, state="done", finished=time.time(),
                                   dst_size=result.output_size, dst=str(result.output))
                library.forget(plan.source.path)
                fresh = _probe_into_library(library, result.output)
                print(color(f"  ✓ {human_size(plan.source.size)} → "
                            f"{human_size(result.output_size)}  "
                            f"saved {human_size(result.saved)}  "
                            f"in {human_duration(result.elapsed)}", "green"))
                print(color(f"    → {result.output.name}", "grey"))
                print(color(f"    source {result.disposal}", "grey"))
                if fresh and fresh.probe_error:
                    warn(f"converted file did not re-probe cleanly: {fresh.probe_error}")
            else:
                failed += 1
                library.update_job(job_id, state="cancelled" if result.cancelled else "failed",
                                   finished=time.time(), error=result.error)
                print(color(f"  ✗ {result.error}", "red"))
                print(color("    source left untouched", "grey"))
    except KeyboardInterrupt:
        print(color("\nAborted.", "red"))
    finally:
        signal.signal(signal.SIGINT, previous)

    elapsed = time.monotonic() - started_all
    print()
    parts = [color(f"{succeeded} converted", "green")]
    if failed:
        parts.append(color(f"{failed} failed", "red"))
    if skipped:
        parts.append(color(f"{skipped} skipped", "yellow"))
    print(color("Done: ", "bold") + ", ".join(parts)
          + f" · reclaimed {human_size(reclaimed)} in {human_duration(elapsed)}")
    return 0 if failed == 0 else 1


def _probe_into_library(library: Library, path: Path) -> VideoFile | None:
    from .probe import probe_file
    try:
        video = probe_file(path)
    except Exception:
        return None
    library.put(video)
    library.conn.commit()
    return video


# --- parser -----------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vidlib",
        description="Scan a video library, group it, and convert 4K files to 1080p.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Typical flow:\n"
            "  vidlib scan ~/Videos          # index the library\n"
            "  vidlib groups --group resolution\n"
            "  vidlib list --4k              # see the 4K files\n"
            "  vidlib select --4k --min-size 20G\n"
            "  vidlib convert --selected     # asks for encoder settings, then converts\n"
        ),
    )
    parser.add_argument("--db", metavar="PATH", help="library database (default: XDG state dir)")
    parser.add_argument("--config", metavar="PATH", help="config file to use")
    parser.add_argument("--no-color", action="store_true", help="disable coloured output")
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan_parser = subparsers.add_parser("scan", help="index directories into the library")
    scan_parser.add_argument("paths", nargs="*", help="directories to scan")
    scan_parser.add_argument("-f", "--force", action="store_true", help="re-probe every file")
    scan_parser.add_argument("--min-size", metavar="SIZE", help="ignore files smaller than this")
    scan_parser.add_argument("-j", "--workers", type=int, default=0, help="parallel probes")
    scan_parser.add_argument("--prune", action="store_true", help="drop entries whose files are gone")
    scan_parser.set_defaults(func=cmd_scan)

    list_parser = subparsers.add_parser("list", help="list files, grouped")
    list_parser.add_argument("-g", "--group", choices=list(GROUPERS), help="group key")
    list_parser.add_argument("--limit", type=int, default=0, help="cap rows shown")
    list_parser.add_argument("--selected", action="store_true", help="only selected files")
    list_parser.add_argument("--paths-only", action="store_true", help="print bare paths")
    add_filter_args(list_parser)
    list_parser.set_defaults(func=cmd_list)

    groups_parser = subparsers.add_parser("groups", help="summarise the library by group")
    groups_parser.add_argument("-g", "--group", choices=list(GROUPERS), help="group key")
    groups_parser.add_argument("--selected", action="store_true", help="only selected files")
    add_filter_args(groups_parser)
    groups_parser.set_defaults(func=cmd_groups)

    dupes_parser = subparsers.add_parser("dupes", help="find titles stored more than once")
    dupes_parser.add_argument("--same-resolution", action="store_true",
                              help="also report duplicates at the same resolution")
    dupes_parser.add_argument("--selected", action="store_true", help="only selected files")
    add_filter_args(dupes_parser)
    dupes_parser.set_defaults(func=cmd_dupes)

    select_parser = subparsers.add_parser("select", help="add files to the saved selection")
    select_parser.add_argument("paths", nargs="*", help="specific files to select")
    select_parser.add_argument("--remove", action="store_true", help="deselect instead")
    select_parser.add_argument("--clear", action="store_true", help="empty the selection")
    select_parser.add_argument("--selected", action="store_true", help=argparse.SUPPRESS)
    add_filter_args(select_parser)
    select_parser.set_defaults(func=cmd_select)

    selection_parser = subparsers.add_parser("selection", help="show the saved selection")
    selection_parser.set_defaults(func=cmd_selection)

    convert_parser = subparsers.add_parser("convert", help="convert files to 1080p")
    convert_parser.add_argument("paths", nargs="*", help="specific files to convert")
    convert_parser.add_argument("--selected", action="store_true", help="convert the saved selection")
    convert_parser.add_argument("-e", "--encoder", choices=list(ENCODERS), help="video encoder")
    convert_parser.add_argument("-q", "--quality", type=int, help="CRF/CQ value (lower is better)")
    convert_parser.add_argument("-p", "--preset", help="encoder preset")
    convert_parser.add_argument("-t", "--target-height", type=int, default=1080,
                                help="output height (default 1080)")
    convert_parser.add_argument("--tonemap", choices=["auto", "always", "never"],
                                help="HDR handling (default auto)")
    convert_parser.add_argument("--disposal", choices=list(trash.DISPOSAL_MODES),
                                help="what to do with the source afterwards")
    convert_parser.add_argument("--quarantine-dir", metavar="DIR",
                                help="directory for --disposal quarantine")
    convert_parser.add_argument("--container-out", metavar="EXT",
                                help="output container, e.g. mkv")
    convert_parser.add_argument("--reencode-audio", action="store_true",
                                help="re-encode audio instead of copying it")
    convert_parser.add_argument("-n", "--dry-run", action="store_true",
                                help="show the plan without converting")
    convert_parser.add_argument("-y", "--yes", action="store_true", help="skip confirmation")
    convert_parser.add_argument("--force", action="store_true",
                                help="convert even if already at or below the target height")
    add_filter_args(convert_parser)
    convert_parser.set_defaults(func=cmd_convert)

    jobs_parser = subparsers.add_parser("jobs", help="show conversion history")
    jobs_parser.add_argument("--state", choices=["pending", "running", "done", "failed", "cancelled"])
    jobs_parser.add_argument("--limit", type=int, default=40)
    jobs_parser.add_argument("--clear", action="store_true", help="forget finished jobs")
    jobs_parser.set_defaults(func=cmd_jobs)

    stats_parser = subparsers.add_parser("stats", help="library overview")
    stats_parser.set_defaults(func=cmd_stats)

    doctor_parser = subparsers.add_parser("doctor", help="check ffmpeg and encoders")
    doctor_parser.add_argument("--probe-hardware", action="store_true",
                               help="actually test each hardware encoder")
    doctor_parser.set_defaults(func=cmd_doctor)

    config_parser = subparsers.add_parser("config", help="show or create the config file")
    config_parser.add_argument("--init", action="store_true", help="write a config file")
    config_parser.add_argument("--force", action="store_true", help="overwrite an existing config")
    config_parser.set_defaults(func=cmd_config)

    roots_parser = subparsers.add_parser("roots", help="manage scan roots")
    roots_parser.add_argument("--add", action="append", metavar="DIR")
    roots_parser.add_argument("--remove", action="append", metavar="DIR")
    roots_parser.set_defaults(func=cmd_roots)

    tui_parser = subparsers.add_parser("tui", help="launch the terminal UI")
    tui_parser.set_defaults(func=cmd_tui)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.no_color:
        from .util import set_color
        set_color(False)
    try:
        config = Config.load(args.config)
    except ValueError as exc:
        return fail(str(exc))
    try:
        return args.func(args, config)
    except FFmpegMissing as exc:
        return fail(str(exc))
    except ValueError as exc:
        # Bad --min-size, unknown group key and similar user input.
        return fail(str(exc))
    except KeyboardInterrupt:
        print(color("\nInterrupted.", "yellow"))
        return 130
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
