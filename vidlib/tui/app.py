# SPDX-License-Identifier: GPL-3.0-or-later
"""Textual terminal UI: browse the library, pick files, convert them."""

from __future__ import annotations

import os
from pathlib import Path

from textual import on, work
from textual.binding import Binding
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button, Checkbox, Footer, Header, Input, Label, ProgressBar, RichLog,
    Select, Static, Tree,
)
from textual.widgets.tree import TreeNode

from ..config import Config
from ..convert import ENCODERS, plan_conversion, run_conversion
from ..db import Library
from ..grouping import GROUPERS, Filter, display_group_name, group_files, summarize
from ..models import VideoFile
from ..probe import available_encoders, probe_file
from ..scanner import scan
from ..util import human_duration, human_size, plural

GROUP_CHOICES = [
    ("Resolution", "resolution"), ("Directory", "directory"), ("Encode", "encode"),
    ("Codec", "codec"), ("Dynamic range", "hdr"), ("Container", "container"),
    ("Bit depth", "depth"), ("Audio", "audio"), ("File size", "size"),
    ("Efficiency", "efficiency"), ("Frame rate", "fps"), ("Year", "year"),
    ("All files", "none"),
]


class LibraryTree(Tree):
    """Tree whose space key selects a file instead of expanding a node.

    Textual's Tree binds space to toggle_node, which would otherwise swallow
    the app-level select binding advertised in the footer.
    """

    BINDINGS = [
        Binding("space", "select_item", "Select", show=False),
        Binding("shift+space", "toggle_node", "Expand/collapse", show=False),
    ]

    def action_select_item(self) -> None:
        self.app.action_toggle()


class SettingsScreen(ModalScreen[dict | None]):
    """Per-batch encoder settings, matching the CLI's ask-per-job behaviour."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, config: Config, file_count: int, total_size: int):
        super().__init__()
        self.config = config
        self.file_count = file_count
        self.total_size = total_size
        self._available = available_encoders()

    def compose(self) -> ComposeResult:
        usable = [n for n in ENCODERS if n in self._available] or list(ENCODERS)
        default = self.config.encoder if self.config.encoder in usable else usable[0]
        spec = ENCODERS[default]
        with Vertical(id="dialog"):
            yield Label(f"[b]Convert {self.file_count} file(s) · "
                        f"{human_size(self.total_size)}[/b]")
            yield Label("Encoder")
            yield Select(
                [(f"{ENCODERS[n].label}", n) for n in usable],
                value=default, allow_blank=False, id="encoder",
            )
            yield Label("", id="encoder-note", classes="hint")
            yield Label("Quality (CRF/CQ — lower is better)")
            yield Input(value=str(spec.default_quality), id="quality", type="integer")
            yield Label("Preset")
            yield Select(
                [(p, p) for p in spec.presets],
                value=spec.default_preset, allow_blank=False, id="preset",
            )
            yield Label("Target height")
            yield Input(value=str(self.config.target_height), id="height", type="integer")
            yield Label("After a verified conversion the source is "
                        f"[b]{self.config.disposal}[/b].", classes="hint")
            with Horizontal(id="dialog-buttons"):
                yield Button("Cancel", variant="default", id="cancel")
                yield Button("Convert", variant="primary", id="ok")

    def on_mount(self) -> None:
        self._refresh_note()

    def _refresh_note(self) -> None:
        name = self.query_one("#encoder", Select).value
        spec = ENCODERS[str(name)]
        self.query_one("#encoder-note", Label).update(spec.notes)

    @on(Select.Changed, "#encoder")
    def _encoder_changed(self, event: Select.Changed) -> None:
        spec = ENCODERS[str(event.value)]
        self.query_one("#quality", Input).value = str(spec.default_quality)
        preset = self.query_one("#preset", Select)
        preset.set_options([(p, p) for p in spec.presets])
        preset.value = spec.default_preset
        self._refresh_note()

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#ok")
    def _confirm(self) -> None:
        def as_int(widget_id: str, fallback: int) -> int:
            try:
                return int(self.query_one(widget_id, Input).value)
            except (ValueError, TypeError):
                return fallback

        encoder = str(self.query_one("#encoder", Select).value)
        self.dismiss({
            "encoder": encoder,
            "quality": as_int("#quality", ENCODERS[encoder].default_quality),
            "preset": str(self.query_one("#preset", Select).value),
            "target_height": as_int("#height", self.config.target_height),
        })


class ConvertScreen(Screen):
    """Runs a batch of conversions, streaming progress into a log."""

    BINDINGS = [("escape", "close", "Close"), ("c", "cancel_batch", "Cancel batch")]

    def __init__(self, videos: list[VideoFile], settings: dict, config: Config,
                 db_path: str | None):
        super().__init__()
        self.videos = videos
        self.settings = settings
        self.config = config
        self.db_path = db_path
        self.cancelled = False
        self.finished = False

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="convert-body"):
            yield Static("", id="convert-current")
            yield ProgressBar(total=100, show_eta=False, id="convert-bar")
            yield RichLog(id="convert-log", markup=True, wrap=True)
        yield Footer()

    def on_mount(self) -> None:
        self.title = "Converting"
        self.run_batch()

    def action_cancel_batch(self) -> None:
        if not self.finished:
            self.cancelled = True
            self.query_one("#convert-log", RichLog).write(
                "[yellow]Cancelling after the current file…[/yellow]"
            )

    def action_close(self) -> None:
        if self.finished:
            self.dismiss()

    @work(thread=True, exclusive=True)
    def run_batch(self) -> None:
        log = self.query_one("#convert-log", RichLog)
        current = self.query_one("#convert-current", Static)
        bar = self.query_one("#convert-bar", ProgressBar)
        library = Library(self.db_path)
        converted = failed = 0
        reclaimed = 0
        try:
            for index, video in enumerate(self.videos, start=1):
                if self.cancelled:
                    self.app.call_from_thread(
                        log.write, f"[yellow]Stopped; {len(self.videos) - index + 1} "
                                   f"file(s) not converted.[/yellow]")
                    break
                plan = plan_conversion(
                    video,
                    encoder=self.settings["encoder"],
                    quality=self.settings["quality"],
                    preset=self.settings["preset"],
                    target_height=self.settings["target_height"],
                    tonemap=self.config.tonemap,
                    copy_audio=self.config.copy_audio,
                    audio_codec=self.config.audio_codec,
                    audio_bitrate=self.config.audio_bitrate,
                    copy_subs=self.config.copy_subs,
                    disposal=self.config.disposal,
                    quarantine_dir=self.config.quarantine_dir or None,
                    container=self.config.container or None,
                    retag_codec=self.config.retag_codec,
                )
                header = f"[{index}/{len(self.videos)}] {video.name}"
                self.app.call_from_thread(current.update, header)
                self.app.call_from_thread(bar.update, progress=0)
                self.app.call_from_thread(log.write, f"[b]{header}[/b]")

                job_id = library.create_job(
                    video.path, str(plan.destination), plan.encoder, plan.quality,
                    plan.preset, video.size, plan.disposal,
                )
                import time as _time
                library.update_job(job_id, state="running", started=_time.time())

                def progress(update, _bar=bar, _cur=current, _header=header):
                    self.app.call_from_thread(_bar.update, progress=update.percent)
                    eta = update.eta_seconds
                    self.app.call_from_thread(
                        _cur.update,
                        f"{_header}\n{update.percent:.1f}%  {update.speed:.1f}x  "
                        f"ETA {human_duration(eta) if eta else '--'}",
                    )

                result = run_conversion(plan, progress=progress)

                if result.ok:
                    converted += 1
                    reclaimed += result.saved
                    library.update_job(job_id, state="done", finished=_time.time(),
                                       dst_size=result.output_size, dst=str(result.output))
                    library.forget(video.path)
                    try:
                        library.put(probe_file(result.output))
                        library.conn.commit()
                    except Exception:
                        pass
                    self.app.call_from_thread(
                        log.write,
                        f"  [green]✓[/green] {human_size(video.size)} → "
                        f"{human_size(result.output_size)} "
                        f"(saved {human_size(result.saved)}) · {result.disposal}")
                else:
                    failed += 1
                    library.update_job(
                        job_id, state="cancelled" if result.cancelled else "failed",
                        finished=_time.time(), error=result.error)
                    self.app.call_from_thread(
                        log.write, f"  [red]✗ {result.error}[/red]  source left untouched")
        finally:
            library.close()

        self.finished = True
        summary = (f"[b]Done:[/b] {converted} converted"
                   + (f", [red]{failed} failed[/red]" if failed else "")
                   + f" · reclaimed {human_size(reclaimed)}")
        self.app.call_from_thread(self.query_one("#convert-log", RichLog).write, summary)
        self.app.call_from_thread(
            self.query_one("#convert-current", Static).update,
            "Finished — press Escape to go back.")


class VidlibApp(App):
    """Browse a scanned library, mark 4K files, convert them to 1080p."""

    CSS_PATH = "app.tcss"
    TITLE = "vidlib"

    BINDINGS = [
        ("space", "toggle", "Select"),
        ("a", "toggle_group", "Select group"),
        ("c", "convert", "Convert"),
        ("A", "select_all_4k", "All 4K"),
        ("x", "clear_selection", "Clear"),
        ("r", "rescan", "Rescan"),
        ("slash", "focus_filter", "Filter"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self, db_path: str | None, config: Config):
        super().__init__()
        self.db_path = db_path
        self.config = config
        self.library = Library(db_path)
        self.videos: list[VideoFile] = []
        self.selected: set[str] = set()
        self._node_files: dict[int, VideoFile] = {}
        self._node_groups: dict[int, list[VideoFile]] = {}

    # --- layout ---------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Horizontal(id="controls"):
            yield Select(GROUP_CHOICES, value=self.config.group,
                         allow_blank=False, id="group")
            yield Input(placeholder="filter by name or path…", id="filter")
            yield Checkbox("4K only", value=True, id="only4k")
        yield LibraryTree("Library", id="tree")
        yield Static("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = str(self.library.path)
        self.reload()
        # Land on the tree, not the group dropdown, so the keys in the footer
        # work straight away.
        self.query_one("#tree", LibraryTree).focus()

    # --- data -----------------------------------------------------------

    def current_filter(self) -> Filter:
        return Filter(
            only_4k=self.query_one("#only4k", Checkbox).value,
            pattern=self.query_one("#filter", Input).value.strip(),
        )

    def reload(self) -> None:
        self.videos = [v for v in self.library.all_files() if not v.probe_error]
        self.selected = set(self.library.selected_paths())
        self.rebuild_tree()

    def rebuild_tree(self) -> None:
        tree = self.query_one("#tree", LibraryTree)
        tree.clear()
        self._node_files.clear()
        self._node_groups.clear()

        matching = self.current_filter().apply(self.videos)
        key = str(self.query_one("#group", Select).value)
        roots = self.library.roots()
        tree.root.label = f"Library — {plural(len(matching), 'file')}"
        tree.root.expand()

        if not matching:
            tree.root.add_leaf(
                "No files match. Press 'r' to scan, or clear the filter."
            )
            self.update_status()
            return

        for name, group in group_files(matching, key):
            size = sum(v.size for v in group)
            node = tree.root.add(
                f"{display_group_name(key, name, roots)}  "
                f"[dim]({plural(len(group), 'file')}, {human_size(size)})[/dim]",
                expand=len(matching) <= 60,
            )
            self._node_groups[node.id] = group
            for video in group:
                leaf = node.add_leaf(self._file_label(video))
                self._node_files[leaf.id] = video
        self.update_status()

    def _file_label(self, video: VideoFile) -> str:
        mark = "[green]●[/green]" if video.path in self.selected else "○"
        flag = "[cyan b]4K[/cyan b]" if video.is_4k else video.resolution
        hdr = f" [magenta]{video.hdr}[/magenta]" if video.is_hdr else ""
        return (f"{mark} {video.name}  [dim]{flag} · {video.encode}{hdr} · "
                f"{human_size(video.size)}[/dim]")

    def _refresh_labels(self) -> None:
        tree = self.query_one("#tree", LibraryTree)
        for node_id, video in self._node_files.items():
            node = tree.get_node_by_id(node_id)
            if node is not None:
                node.set_label(self._file_label(video))

    def update_status(self) -> None:
        chosen = [v for v in self.videos if v.path in self.selected]
        saving = sum(
            v.estimated_saving(self.config.encoder, self.config.target_height)
            for v in chosen
        )
        info = summarize(self.videos)
        message = (
            f"Selected [b]{len(chosen)}[/b] · {human_size(sum(v.size for v in chosen))} "
            f"· est. saving [green]{human_size(saving)}[/green]\n"
            f"[dim]Library: {info['count']} files, {human_size(info['size'])}; "
            f"{info['count_4k']} in 4K ({human_size(info['size_4k'])})[/dim]"
        )
        self.query_one("#status", Static).update(message)

    # --- events ---------------------------------------------------------

    @on(Select.Changed, "#group")
    def _group_changed(self) -> None:
        self.rebuild_tree()

    @on(Checkbox.Changed, "#only4k")
    def _only4k_changed(self) -> None:
        self.rebuild_tree()

    @on(Input.Changed, "#filter")
    def _filter_changed(self) -> None:
        self.rebuild_tree()

    @on(Input.Submitted, "#filter")
    def _filter_submitted(self) -> None:
        self.query_one("#tree", LibraryTree).focus()

    @on(Tree.NodeSelected, "#tree")
    def _node_selected(self, event: Tree.NodeSelected) -> None:
        # Enter on a file toggles it; on a group Textual already expands it.
        if event.node.id in self._node_files:
            self.action_toggle()

    # --- actions --------------------------------------------------------

    def _focused_node(self) -> TreeNode | None:
        return self.query_one("#tree", LibraryTree).cursor_node

    def action_toggle(self) -> None:
        node = self._focused_node()
        if node is None:
            return
        if node.id in self._node_files:
            video = self._node_files[node.id]
            if video.path in self.selected:
                self.library.deselect([video.path])
                self.selected.discard(video.path)
            else:
                self.library.select([video.path])
                self.selected.add(video.path)
            node.set_label(self._file_label(video))
            self.update_status()
        elif node.id in self._node_groups:
            self.action_toggle_group()

    def action_toggle_group(self) -> None:
        node = self._focused_node()
        if node is None:
            return
        group = self._node_groups.get(node.id)
        if group is None and node.parent is not None:
            group = self._node_groups.get(node.parent.id)
        if not group:
            return
        paths = [v.path for v in group]
        if all(p in self.selected for p in paths):
            self.library.deselect(paths)
            self.selected.difference_update(paths)
        else:
            self.library.select(paths)
            self.selected.update(paths)
        self._refresh_labels()
        self.update_status()

    def action_select_all_4k(self) -> None:
        paths = [v.path for v in self.videos if v.is_4k]
        if not paths:
            self.notify("No 4K files in the library.", severity="warning")
            return
        self.library.select(paths)
        self.selected.update(paths)
        self._refresh_labels()
        self.update_status()
        self.notify(f"Selected {len(paths)} 4K file(s).")

    def action_clear_selection(self) -> None:
        self.library.clear_selection()
        self.selected.clear()
        self._refresh_labels()
        self.update_status()

    def action_focus_filter(self) -> None:
        self.query_one("#filter", Input).focus()

    def action_rescan(self) -> None:
        roots = self.library.roots()
        if not roots:
            self.notify("No roots configured. Run 'vidlib scan <dir>' first.",
                        severity="warning")
            return
        self.notify(f"Scanning {len(roots)} root(s)…")
        self._do_rescan(roots)

    @work(thread=True, exclusive=True)
    def _do_rescan(self, roots: list[str]) -> None:
        library = Library(self.db_path)
        try:
            from ..util import parse_size
            stats = scan(roots, library,
                         min_size=parse_size(self.config.min_size or "0"),
                         include_hidden=self.config.include_hidden,
                         follow_symlinks=self.config.follow_symlinks)
        except Exception as exc:
            self.call_from_thread(self.notify, f"Scan failed: {exc}", severity="error")
            return
        finally:
            library.close()
        self.call_from_thread(self.notify, f"Scan complete: {stats}")
        self.call_from_thread(self.reload)

    def action_convert(self) -> None:
        chosen = [v for v in self.videos if v.path in self.selected]
        if not chosen:
            self.notify("Nothing selected. Press space to select files.",
                        severity="warning")
            return

        def start(settings: dict | None) -> None:
            if not settings:
                return
            self.push_screen(
                ConvertScreen(chosen, settings, self.config, self.db_path),
                lambda _=None: self.reload(),
            )

        self.push_screen(
            SettingsScreen(self.config, len(chosen), sum(v.size for v in chosen)),
            start,
        )

    def on_unmount(self) -> None:
        try:
            self.library.close()
        except Exception:
            pass


def run_tui(db_path: str | None = None, config: Config | None = None) -> int:
    app = VidlibApp(db_path=db_path, config=config or Config())
    app.run()
    return 0
