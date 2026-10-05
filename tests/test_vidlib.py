# SPDX-License-Identifier: GPL-3.0-or-later
"""Test suite for vidlib. Run with:  python3 -m unittest discover -s tests -v

Tests that need ffmpeg generate their own tiny fixtures and are skipped
automatically when ffmpeg is unavailable.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vidlib import trash
from vidlib.config import Config
from vidlib.convert import (
    ENCODERS, build_filters, decide_hdr_handling, output_name, plan_conversion,
    run_conversion,
)
from vidlib.db import Library
from vidlib.grouping import (
    Filter, display_group_name, find_duplicates, group_files, summarize,
)
from vidlib.models import (
    SubtitleTrack, VideoFile, classify_hdr, classify_resolution, bit_depth_from_pix_fmt,
)
from vidlib.probe import probe_file
from vidlib.scanner import scan, walk_videos
from vidlib.subtitles import find_sidecar_subtitles, is_english
from vidlib.util import human_duration, human_size, parse_size, render_table, truncate

HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def make_video(path: Path, width: int, height: int, *, codec: str = "libx264",
               duration: float = 1.0, hdr: str | None = None, audio: bool = True) -> Path:
    """Render a tiny real video file for tests."""
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", f"testsrc2=size={width}x{height}:rate=24:duration={duration}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}"]
    if hdr:
        transfer = "smpte2084" if hdr == "HDR10" else "arib-std-b67"
        cmd += ["-vf", f"setparams=color_primaries=bt2020:color_trc={transfer}:colorspace=bt2020nc",
                "-pix_fmt", "yuv420p10le"]
    else:
        cmd += ["-pix_fmt", "yuv420p"]
    cmd += ["-c:v", codec, "-preset", "ultrafast"]
    if codec == "libx265":
        cmd += ["-x265-params", "log-level=0"]
    if audio:
        cmd += ["-c:a", "aac", "-b:a", "64k"]
    cmd += [str(path)]
    subprocess.run(cmd, check=True, capture_output=True)
    return path


def make_subtitle(path: Path, text: str = "1\n00:00:00,000 --> 00:00:01,000\nHello\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def make_video_with_subtitles(path: Path, langs: list[str | None], *, width: int = 640,
                              height: int = 360, duration: float = 1.0) -> Path:
    """A video with one embedded subtitle stream per entry in `langs`
    (None leaves that stream untagged)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    srt_paths = [make_subtitle(path.parent / f"{path.stem}.tmp{i}.srt") for i in range(len(langs))]
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", f"testsrc2=size={width}x{height}:rate=24:duration={duration}",
           "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}"]
    for srt in srt_paths:
        cmd += ["-i", str(srt)]
    cmd += ["-map", "0:v", "-map", "1:a"]
    cmd += [arg for i in range(len(langs)) for arg in ("-map", f"{i + 2}:0")]
    cmd += ["-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast",
            "-c:a", "aac", "-b:a", "64k", "-c:s", "srt"]
    for i, lang in enumerate(langs):
        if lang:
            cmd += [f"-metadata:s:s:{i}", f"language={lang}"]
    cmd += [str(path)]
    subprocess.run(cmd, check=True, capture_output=True)
    for srt in srt_paths:
        srt.unlink(missing_ok=True)
    return path


class TestUtil(unittest.TestCase):
    def test_human_size(self):
        self.assertEqual(human_size(0), "0 B")
        self.assertEqual(human_size(512), "512 B")
        self.assertEqual(human_size(1024), "1.00 KB")
        self.assertEqual(human_size(1234567890), "1.15 GB")
        self.assertEqual(human_size(None), "-")

    def test_parse_size(self):
        self.assertEqual(parse_size("10G"), 10 * 1024**3)
        self.assertEqual(parse_size("500mb"), 500 * 1024**2)
        self.assertEqual(parse_size("1.5 TiB"), int(1.5 * 1024**4))
        self.assertEqual(parse_size("2048"), 2048)
        with self.assertRaises(ValueError):
            parse_size("banana")

    def test_human_duration(self):
        self.assertEqual(human_duration(8130), "2:15:30")
        self.assertEqual(human_duration(90), "1:30")
        self.assertEqual(human_duration(None), "-")

    def test_truncate_keeps_both_ends(self):
        out = truncate("Blade.Runner.2049.2160p.HDR.mkv", 20)
        self.assertEqual(len(out), 20)
        self.assertTrue(out.startswith("Blade"))
        self.assertTrue(out.endswith(".mkv"))

    def test_render_table_fits_width(self):
        rows = [["x" * 200, "1 GB"]]
        table = render_table(["Name", "Size"], rows, align="lr", flex_column=0, max_width=60)
        for line in table.splitlines():
            self.assertLessEqual(len(line), 60)


class TestClassification(unittest.TestCase):
    def test_resolution_buckets(self):
        self.assertEqual(classify_resolution(3840, 2160), "2160p")
        self.assertEqual(classify_resolution(3840, 1600), "2160p")   # 2.40:1 scope
        self.assertEqual(classify_resolution(4096, 1716), "2160p")   # DCI 4K
        self.assertEqual(classify_resolution(1920, 1080), "1080p")
        self.assertEqual(classify_resolution(1920, 800), "1080p")
        self.assertEqual(classify_resolution(1280, 720), "720p")
        self.assertEqual(classify_resolution(720, 480), "480p")
        self.assertEqual(classify_resolution(None, None), "unknown")

    def test_hdr_detection(self):
        self.assertEqual(classify_hdr("smpte2084", "bt2020", False), "HDR10")
        self.assertEqual(classify_hdr("arib-std-b67", "bt2020", False), "HLG")
        self.assertEqual(classify_hdr("bt709", "bt709", False), "SDR")
        self.assertEqual(classify_hdr(None, None, True), "DolbyVision")
        # Partial metadata: wide gamut plus 10-bit still counts as HDR.
        self.assertEqual(classify_hdr(None, None, False, "bt2020nc", 10), "HDR")
        self.assertEqual(classify_hdr(None, None, False, "bt2020nc", 8), "SDR")

    def test_bit_depth(self):
        self.assertEqual(bit_depth_from_pix_fmt("yuv420p10le"), 10)
        self.assertEqual(bit_depth_from_pix_fmt("yuv420p"), 8)
        self.assertEqual(bit_depth_from_pix_fmt("p010le"), 10)
        self.assertIsNone(bit_depth_from_pix_fmt(None))

    def test_title_key_normalises_naming_styles(self):
        names = [
            "Blade.Runner.2049.2017.2160p.UHD.BluRay.x265.10bit.HDR.mkv",
            "Blade Runner 2049 (2017) 1080p BluRay x264.mkv",
            "Blade Runner 2049 [2017] [2160p] [HDR].mkv",
        ]
        keys = {VideoFile(path=f"/m/{n}", size=0, mtime=0).title_key for n in names}
        self.assertEqual(len(keys), 1, f"expected one key, got {keys}")
        self.assertEqual(keys.pop(), "blade runner 2049 (2017)")


class TestModel(unittest.TestCase):
    def sample(self, **kw) -> VideoFile:
        base = dict(
            path="/m/Movie.2160p.mkv", size=30 * 1024**3, mtime=0.0, duration=7200,
            vcodec="hevc", width=3840, height=2160, pix_fmt="yuv420p10le", fps=24.0,
            color_transfer="smpte2084", color_primaries="bt2020", acodec="truehd",
            achannels=8, n_audio=2, abitrate_total=4_600_000,
        )
        base.update(kw)
        return VideoFile(**base)

    def test_derived_properties(self):
        v = self.sample()
        self.assertTrue(v.is_4k)
        self.assertTrue(v.is_hdr)
        self.assertEqual(v.encode, "hevc 10bit")
        self.assertEqual(v.bit_depth, 10)
        self.assertAlmostEqual(v.overall_bitrate, 30 * 1024**3 * 8 / 7200)

    def test_estimate_never_exceeds_source(self):
        v = self.sample(size=100, duration=7200)
        self.assertLessEqual(v.estimated_output_size(), 100)
        self.assertEqual(v.estimated_saving(), 0)

    def test_lossless_audio_dominates_estimate(self):
        v = self.sample()
        copied = v.estimated_output_size(copy_audio=True)
        reencoded = v.estimated_output_size(copy_audio=False)
        self.assertGreater(copied, reencoded)

    def test_json_roundtrip_tolerates_unknown_fields(self):
        v = self.sample()
        blob = v.to_json().replace("{", '{"a_field_from_the_future": 1,', 1)
        self.assertEqual(VideoFile.from_json(blob).path, v.path)


class TestOutputNaming(unittest.TestCase):
    def test_replaces_resolution_tags(self):
        self.assertEqual(
            output_name(Path("/m/Dune.2024.2160p.WEB-DL.H264.mkv"), encoder="libx265").name,
            "Dune.2024.1080p.WEB-DL.x265.mkv")

    def test_collapses_duplicate_labels(self):
        self.assertEqual(
            output_name(Path("/m/Movie.2160p.UHD.mkv"), retag_codec=False).name,
            "Movie.1080p.mkv")

    def test_appends_when_no_tag_present(self):
        self.assertEqual(output_name(Path("/m/Movie.mkv"), retag_codec=False).name,
                         "Movie.1080p.mkv")

    def test_never_collides_with_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "Movie.1080p.mkv"
            src.write_bytes(b"x")
            out = output_name(src, retag_codec=False)
            self.assertNotEqual(out.resolve(), src.resolve())

    def test_hdr_tag_rewritten_when_tonemapping(self):
        name = output_name(Path("/m/Movie.2160p.HDR10.mkv"), tonemapped=True,
                           retag_codec=False).name
        self.assertIn("SDR", name)
        self.assertNotIn("HDR", name)

    def test_target_height_respected(self):
        self.assertEqual(output_name(Path("/m/Movie.2160p.mkv"), 720, retag_codec=False).name,
                         "Movie.720p.mkv")


class TestHdrPolicy(unittest.TestCase):
    def video(self, hdr=True):
        return VideoFile(path="/m/a.mkv", size=1, mtime=0, width=3840, height=2160,
                         pix_fmt="yuv420p10le",
                         color_transfer="smpte2084" if hdr else "bt709",
                         color_primaries="bt2020" if hdr else "bt709")

    def test_hevc_keeps_hdr_but_h264_tonemaps(self):
        self.assertEqual(decide_hdr_handling(self.video(), "libx265"), (False, True))
        self.assertEqual(decide_hdr_handling(self.video(), "libx264"), (True, False))

    def test_forced_modes(self):
        self.assertEqual(decide_hdr_handling(self.video(), "libx265", "always"), (True, False))
        self.assertEqual(decide_hdr_handling(self.video(), "libx264", "never"), (False, True))

    def test_sdr_source_is_untouched(self):
        self.assertEqual(decide_hdr_handling(self.video(hdr=False), "libx265"), (False, False))

    def test_scale_filter_never_upscales(self):
        v = self.video(hdr=False)
        plan = plan_conversion(v, target_height=1080)
        self.assertIn("min(1920,iw)", build_filters(plan))
        self.assertIn("min(1080,ih)", build_filters(plan))


class TestGrouping(unittest.TestCase):
    def setUp(self):
        def mk(name, w, h, codec, size, hdr=False):
            return VideoFile(
                path=f"/lib/{name}", size=size, mtime=0, duration=3600, vcodec=codec,
                width=w, height=h, fps=24.0, pix_fmt="yuv420p10le" if hdr else "yuv420p",
                color_transfer="smpte2084" if hdr else "bt709",
                color_primaries="bt2020" if hdr else "bt709")
        self.files = [
            mk("Dune.2024.2160p.mkv", 3840, 2160, "hevc", 30 * 1024**3, hdr=True),
            mk("Dune 2024 1080p.mkv", 1920, 1080, "hevc", 8 * 1024**3),
            mk("Arrival.2016.2160p.mkv", 3840, 2160, "h264", 40 * 1024**3),
            mk("Show.S01E01.720p.mkv", 1280, 720, "h264", 1 * 1024**3),
        ]

    def test_group_by_resolution_is_ordered_best_first(self):
        names = [name for name, _ in group_files(self.files, "resolution")]
        self.assertEqual(names, ["2160p", "1080p", "720p"])

    def test_filters(self):
        self.assertEqual(len(Filter(only_4k=True).apply(self.files)), 2)
        self.assertEqual(len(Filter(only_hdr=True).apply(self.files)), 1)
        self.assertEqual(len(Filter(codecs={"h264"}).apply(self.files)), 2)
        self.assertEqual(len(Filter(min_size=20 * 1024**3).apply(self.files)), 2)
        self.assertEqual(len(Filter(pattern="dune").apply(self.files)), 2)

    def test_probe_errors_are_hidden_by_default(self):
        broken = VideoFile(path="/lib/x.mkv", size=1, mtime=0, probe_error="bad")
        self.assertEqual(len(Filter().apply([broken])), 0)
        self.assertEqual(len(Filter(include_errors=True).apply([broken])), 1)

    def test_duplicates_prefer_highest_resolution(self):
        dupes = find_duplicates(self.files)
        self.assertEqual(len(dupes), 1)
        self.assertEqual(dupes[0].files[0].resolution, "2160p")
        self.assertEqual(dupes[0].reclaimable, 8 * 1024**3)

    def test_summarize(self):
        info = summarize(self.files)
        self.assertEqual(info["count"], 4)
        self.assertEqual(info["count_4k"], 2)

    def test_display_group_name_shortens_directories(self):
        self.assertEqual(
            display_group_name("directory", "/media/Movies/Dune", ["/media"]),
            "media/Movies/Dune")
        self.assertEqual(display_group_name("resolution", "2160p", ["/media"]), "2160p")


class TestLibraryDb(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.lib = Library(Path(self.tmp) / "test.db")

    def tearDown(self):
        self.lib.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def video(self, path="/m/a.mkv", size=100):
        return VideoFile(path=path, size=size, mtime=1000.0, width=3840, height=2160)

    def test_put_and_get(self):
        v = self.video()
        self.lib.put(v)
        self.lib.conn.commit()
        self.assertEqual(self.lib.get("/m/a.mkv").size, 100)
        self.assertEqual(self.lib.count(), 1)

    def test_freshness_tracks_size_and_mtime(self):
        v = self.video()
        self.lib.put_many([v])
        self.assertTrue(self.lib.is_fresh("/m/a.mkv", 100, 1000.0))
        self.assertFalse(self.lib.is_fresh("/m/a.mkv", 999, 1000.0))
        self.assertFalse(self.lib.is_fresh("/m/a.mkv", 100, 5000.0))
        self.assertFalse(self.lib.is_fresh("/m/missing.mkv", 100, 1000.0))

    def test_selection_roundtrip(self):
        self.lib.put_many([self.video()])
        self.lib.select(["/m/a.mkv"])
        self.assertEqual(len(self.lib.selected_files()), 1)
        self.assertTrue(self.lib.is_selected("/m/a.mkv"))
        self.assertFalse(self.lib.toggle("/m/a.mkv"))
        self.assertEqual(self.lib.selected_paths(), [])

    def test_forget_clears_selection_too(self):
        self.lib.put_many([self.video()])
        self.lib.select(["/m/a.mkv"])
        self.lib.forget("/m/a.mkv")
        self.assertEqual(self.lib.count(), 0)
        self.assertEqual(self.lib.selected_paths(), [])

    def test_jobs(self):
        jid = self.lib.create_job("/m/a.mkv", "/m/b.mkv", "libx265", 20, "medium", 100, "trash")
        self.lib.update_job(jid, state="done", dst_size=40)
        job = self.lib.jobs()[0]
        self.assertEqual(job["state"], "done")
        self.assertEqual(job["dst_size"], 40)
        self.assertEqual(self.lib.clear_jobs(), 1)

    def test_roots(self):
        with tempfile.TemporaryDirectory() as d:
            self.lib.add_root(d)
            self.assertIn(str(Path(d).resolve()), self.lib.roots())
            self.lib.remove_root(d)
            self.assertEqual(self.lib.roots(), [])


class TestConfig(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            Config(roots=["/a", "/b"], quality=21, copy_audio=False).save(path)
            back = Config.load(path)
            self.assertEqual(back.roots, ["/a", "/b"])
            self.assertEqual(back.quality, 21)
            self.assertFalse(back.copy_audio)

    def test_missing_file_gives_defaults(self):
        self.assertEqual(Config.load("/nonexistent/config.toml").encoder, "libx265")

    def test_bad_toml_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.toml"
            path.write_text("this is not = = toml")
            with self.assertRaises(ValueError):
                Config.load(path)


class TestTrash(unittest.TestCase):
    """Disposal is the destructive step, so it is tested in an isolated HOME."""

    def setUp(self):
        # Use the real home filesystem so the same-device branch is exercised.
        self.box = Path(tempfile.mkdtemp(prefix="vidlib-test-", dir=os.path.expanduser("~")))
        self._old_xdg = os.environ.get("XDG_DATA_HOME")
        os.environ["XDG_DATA_HOME"] = str(self.box / "data")

    def tearDown(self):
        if self._old_xdg is None:
            os.environ.pop("XDG_DATA_HOME", None)
        else:
            os.environ["XDG_DATA_HOME"] = self._old_xdg
        shutil.rmtree(self.box, ignore_errors=True)

    def victim(self, name="Movie.mkv") -> Path:
        path = self.box / name
        path.write_bytes(b"x" * 128)
        return path

    def test_freedesktop_trash_writes_restore_record(self):
        victim = self.victim()
        original = str(victim.resolve())
        dest = trash._freedesktop_trash(victim)
        self.assertTrue(Path(dest).exists())
        self.assertFalse(victim.exists())
        info = Path(dest).parent.parent / "info" / f"{Path(dest).name}.trashinfo"
        self.assertTrue(info.exists())
        text = info.read_text()
        self.assertIn("[Trash Info]", text)
        self.assertIn(original, text)

    def test_name_collisions_get_unique_names(self):
        names = {Path(trash._freedesktop_trash(self.victim())).name for _ in range(3)}
        self.assertEqual(len(names), 3)

    def test_quarantine_and_delete_and_keep(self):
        quarantine_dir = self.box / "quar"
        result = trash.dispose(self.victim("a.mkv"), "quarantine",
                               quarantine_dir=quarantine_dir)
        self.assertIn("quarantined", result)
        self.assertTrue((quarantine_dir / "a.mkv").exists())

        victim = self.victim("b.mkv")
        trash.dispose(victim, "delete")
        self.assertFalse(victim.exists())

        victim = self.victim("c.mkv")
        self.assertEqual(trash.dispose(victim, "keep"), "kept")
        self.assertTrue(victim.exists())

    def test_errors(self):
        with self.assertRaises(trash.DisposalError):
            trash.dispose(self.box / "nope.mkv", "trash")
        with self.assertRaises(trash.DisposalError):
            trash.dispose(self.victim("d.mkv"), "not-a-mode")
        with self.assertRaises(trash.DisposalError):
            trash.dispose(self.victim("e.mkv"), "quarantine")

    def test_cross_device_falls_back_to_home_trash(self):
        with tempfile.TemporaryDirectory(prefix="vidlib-xdev-") as other:
            candidates = trash._candidate_trash_dirs(Path(other) / "x.mkv")
            if os.stat(other).st_dev != os.stat(os.path.expanduser("~")).st_dev:
                self.assertEqual(len(candidates), 2)
                self.assertEqual(candidates[-1], trash._home_trash())


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg is required")
class TestProbeAndScan(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="vidlib-scan-"))
        make_video(cls.tmp / "Movies" / "Big.2160p.mkv", 1280, 720, codec="libx265", hdr="HDR10")
        make_video(cls.tmp / "Movies" / "Small.1080p.mp4", 640, 360)
        (cls.tmp / "Movies" / "notes.txt").write_text("not a video")
        (cls.tmp / "Movies" / "broken.mkv").write_bytes(b"GARBAGE")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_walk_finds_only_video_extensions(self):
        found = {e.name for e in walk_videos(self.tmp)}
        self.assertIn("Big.2160p.mkv", found)
        self.assertIn("Small.1080p.mp4", found)
        self.assertNotIn("notes.txt", found)

    def test_probe_reads_metadata(self):
        v = probe_file(self.tmp / "Movies" / "Big.2160p.mkv")
        self.assertIsNone(v.probe_error)
        self.assertEqual(v.vcodec, "hevc")
        self.assertEqual(v.hdr, "HDR10")
        self.assertEqual(v.bit_depth, 10)
        self.assertGreater(v.duration, 0)

    def test_broken_file_reports_error_without_raising(self):
        v = probe_file(self.tmp / "Movies" / "broken.mkv")
        self.assertIsNotNone(v.probe_error)

    def test_missing_file(self):
        v = probe_file(self.tmp / "nope.mkv")
        self.assertIsNotNone(v.probe_error)

    def test_scan_caches_results(self):
        with tempfile.TemporaryDirectory() as db_dir:
            with Library(Path(db_dir) / "l.db") as lib:
                first = scan([self.tmp], lib)
                self.assertEqual(first.probed, 2)
                self.assertEqual(first.errors, 1)
                second = scan([self.tmp], lib)
                self.assertEqual(second.probed, 0)
                self.assertEqual(second.cached, 3)
                forced = scan([self.tmp], lib, force=True)
                self.assertEqual(forced.probed, 2)

    def test_scan_min_size_filter(self):
        with tempfile.TemporaryDirectory() as db_dir:
            with Library(Path(db_dir) / "l.db") as lib:
                stats = scan([self.tmp], lib, min_size=10 * 1024**3)
                self.assertEqual(stats.found, 0)
                self.assertGreater(stats.skipped_small, 0)

    def test_prune_missing(self):
        with tempfile.TemporaryDirectory() as db_dir:
            with Library(Path(db_dir) / "l.db") as lib:
                scan([self.tmp], lib)
                lib.put(VideoFile(path="/definitely/not/here.mkv", size=1, mtime=0))
                lib.conn.commit()
                self.assertIn("/definitely/not/here.mkv", lib.prune_missing())


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg is required")
class TestConversion(unittest.TestCase):
    """Real encodes against real files, including the destructive path."""

    def setUp(self):
        self.box = Path(tempfile.mkdtemp(prefix="vidlib-conv-", dir=os.path.expanduser("~")))
        self._old_xdg = os.environ.get("XDG_DATA_HOME")
        os.environ["XDG_DATA_HOME"] = str(self.box / "data")

    def tearDown(self):
        if self._old_xdg is None:
            os.environ.pop("XDG_DATA_HOME", None)
        else:
            os.environ["XDG_DATA_HOME"] = self._old_xdg
        shutil.rmtree(self.box, ignore_errors=True)

    def convert(self, source: Path, **kw):
        video = probe_file(source)
        allow_larger = kw.pop("allow_larger", True)
        plan = plan_conversion(video, preset="ultrafast", quality=32, **kw)
        return plan, run_conversion(plan, allow_larger=allow_larger)

    def test_downscales_and_trashes_source(self):
        src = make_video(self.box / "Movie.2160p.mkv", 1920, 1080)
        plan, result = self.convert(src, target_height=540, disposal="trash")
        self.assertTrue(result.ok, result.error)
        self.assertTrue(result.output.exists())
        self.assertFalse(src.exists(), "source should have been trashed")
        out = probe_file(result.output)
        self.assertEqual(out.height, 540)
        self.assertAlmostEqual(out.duration, 1.0, delta=0.3)

    def test_keep_disposal_leaves_source(self):
        src = make_video(self.box / "Keep.2160p.mkv", 1280, 720)
        _, result = self.convert(src, target_height=360, disposal="keep")
        self.assertTrue(result.ok, result.error)
        self.assertTrue(src.exists())

    def test_audio_and_duration_survive(self):
        src = make_video(self.box / "Audio.2160p.mkv", 640, 360, duration=2.0)
        _, result = self.convert(src, target_height=180, disposal="keep")
        out = probe_file(result.output)
        self.assertEqual(out.n_audio, 1)
        self.assertAlmostEqual(out.duration, 2.0, delta=0.3)

    def test_hdr_preserved_for_hevc(self):
        src = make_video(self.box / "Hdr.2160p.mkv", 1280, 720, codec="libx265", hdr="HDR10")
        _, result = self.convert(src, encoder="libx265", target_height=360, disposal="keep")
        self.assertTrue(result.ok, result.error)
        self.assertEqual(probe_file(result.output).hdr, "HDR10")

    def test_hdr_tonemapped_for_h264(self):
        src = make_video(self.box / "Tm.2160p.mkv", 1280, 720, codec="libx265", hdr="HDR10")
        _, result = self.convert(src, encoder="libx264", target_height=360, disposal="keep")
        self.assertTrue(result.ok, result.error)
        out = probe_file(result.output)
        self.assertEqual(out.hdr, "SDR")
        self.assertEqual(out.bit_depth, 8, "10-bit H.264 hurts player compatibility")

    def test_never_upscales(self):
        src = make_video(self.box / "Small.mkv", 640, 360)
        _, result = self.convert(src, target_height=1080, disposal="keep")
        self.assertTrue(result.ok, result.error)
        self.assertEqual(probe_file(result.output).height, 360)

    def test_dry_run_touches_nothing(self):
        src = make_video(self.box / "Dry.2160p.mkv", 640, 360)
        video = probe_file(src)
        plan = plan_conversion(video, disposal="trash")
        result = run_conversion(plan, dry_run=True)
        self.assertTrue(result.ok)
        self.assertTrue(src.exists())
        self.assertFalse(plan.destination.exists())

    def test_larger_output_is_rejected_and_source_kept(self):
        src = make_video(self.box / "Grow.2160p.mkv", 320, 180, codec="libx265")
        video = probe_file(src)
        plan = plan_conversion(video, encoder="libx264", quality=1, preset="ultrafast",
                               target_height=180, disposal="trash")
        result = run_conversion(plan, allow_larger=False)
        self.assertFalse(result.ok)
        self.assertIn("not smaller", result.error)
        self.assertTrue(src.exists(), "source must survive a rejected conversion")
        self.assertFalse(plan.temp.exists())

    def test_cancellation_keeps_source_and_cleans_temp(self):
        src = make_video(self.box / "Cancel.2160p.mkv", 640, 360)
        video = probe_file(src)
        plan = plan_conversion(video, target_height=180, disposal="trash")
        result = run_conversion(plan, should_cancel=lambda: True)
        self.assertTrue(result.cancelled)
        self.assertTrue(src.exists())
        self.assertFalse(plan.temp.exists())

    def test_failed_encode_keeps_source(self):
        src = make_video(self.box / "Fail.2160p.mkv", 640, 360)
        video = probe_file(src)
        plan = plan_conversion(video, disposal="trash")
        plan.extra_args = ["-not-a-real-flag"]
        result = run_conversion(plan)
        self.assertFalse(result.ok)
        self.assertTrue(src.exists())
        self.assertFalse(plan.temp.exists())

    @unittest.skipUnless(HAVE_FFMPEG, "needs ffmpeg")
    def test_embedded_subtitles_filtered_to_english_by_default(self):
        src = make_video_with_subtitles(self.box / "Subs.mkv", ["eng", "fre"])
        video = probe_file(src)
        self.assertEqual(len(video.subtitle_tracks), 2)
        _, result = self.convert(src, target_height=180, disposal="keep")
        self.assertTrue(result.ok, result.error)
        out = probe_file(result.output)
        self.assertEqual(out.n_subs, 1)
        self.assertEqual(out.sub_langs, ["eng"])

    @unittest.skipUnless(HAVE_FFMPEG, "needs ffmpeg")
    def test_embedded_untagged_subtitle_kept_by_default(self):
        src = make_video_with_subtitles(self.box / "Untagged.mkv", [None, "spa"])
        _, result = self.convert(src, target_height=180, disposal="keep")
        self.assertTrue(result.ok, result.error)
        self.assertEqual(probe_file(result.output).n_subs, 1)

    @unittest.skipUnless(HAVE_FFMPEG, "needs ffmpeg")
    def test_sub_language_none_keeps_every_subtitle(self):
        src = make_video_with_subtitles(self.box / "All.mkv", ["eng", "fre"])
        _, result = self.convert(src, target_height=180, disposal="keep", sub_language=None)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(probe_file(result.output).n_subs, 2)

    @unittest.skipUnless(HAVE_FFMPEG, "needs ffmpeg")
    def test_external_english_subtitle_muxed_in_and_foreign_discarded(self):
        src = make_video(self.box / "Ext.mkv", 640, 360)
        make_subtitle(self.box / "Ext.srt")
        make_subtitle(self.box / "Ext.fr.srt")
        _, result = self.convert(src, target_height=180, disposal="trash")
        self.assertTrue(result.ok, result.error)
        self.assertEqual(probe_file(result.output).n_subs, 1)
        self.assertFalse((self.box / "Ext.fr.srt").exists(),
                         "the French sidecar should have been trashed with the source")

    @unittest.skipUnless(HAVE_FFMPEG, "needs ffmpeg")
    def test_stereo_downmix_forces_two_channels(self):
        src = self.box / "Surround.mkv"
        src.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=24:duration=1",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-ac", "6", "-pix_fmt", "yuv420p",
            "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-b:a", "384k",
            str(src),
        ], check=True, capture_output=True)
        self.assertEqual(probe_file(src).achannels, 6)
        _, result = self.convert(src, target_height=180, disposal="keep", audio_channels=2)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(probe_file(result.output).achannels, 2)

    @unittest.skipUnless(HAVE_FFMPEG, "needs ffmpeg")
    def test_lossless_copy_is_overridden_by_audio_channels(self):
        """copy_audio=True can't change the channel count; audio_channels
        must win, not silently be ignored."""
        src = self.box / "Copy.mkv"
        src.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=24:duration=1",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-ac", "6", "-pix_fmt", "yuv420p",
            "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-b:a", "384k",
            str(src),
        ], check=True, capture_output=True)
        _, result = self.convert(src, target_height=180, disposal="keep",
                                 copy_audio=True, audio_channels=2)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(probe_file(result.output).achannels, 2)


class TestSubtitleSelection(unittest.TestCase):
    """Pure logic: no ffmpeg needed, so these always run."""

    def test_is_english(self):
        self.assertTrue(is_english(None))
        self.assertTrue(is_english(""))
        self.assertTrue(is_english("und"))
        self.assertTrue(is_english("eng"))
        self.assertTrue(is_english("en"))
        self.assertTrue(is_english("ENG"))
        self.assertFalse(is_english("fre"))
        self.assertFalse(is_english("spa"))
        self.assertFalse(is_english("jpn"))

    def test_find_sidecar_subtitles(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            video = base / "Movie.mkv"
            video.touch()
            make_subtitle(base / "Movie.srt")
            make_subtitle(base / "Movie.en.srt")
            make_subtitle(base / "Movie.fr.srt")
            make_subtitle(base / "Other.srt")  # unrelated video, must be ignored

            found = {s.path.name: s for s in find_sidecar_subtitles(video)}
            self.assertEqual(set(found), {"Movie.srt", "Movie.en.srt", "Movie.fr.srt"})
            self.assertTrue(found["Movie.srt"].is_english)
            self.assertTrue(found["Movie.en.srt"].is_english)
            self.assertFalse(found["Movie.fr.srt"].is_english)

    def test_find_sidecar_subtitles_none_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "Lonely.mkv"
            video.touch()
            self.assertEqual(find_sidecar_subtitles(video), [])


class TestSubtitleTrackSerialisation(unittest.TestCase):
    def test_json_roundtrip_preserves_subtitle_tracks(self):
        video = VideoFile(
            path="/x/y.mkv", size=10, mtime=0.0,
            subtitle_tracks=[SubtitleTrack(index=2, language="eng", codec="subrip")],
        )
        restored = VideoFile.from_json(video.to_json())
        self.assertEqual(restored.subtitle_tracks, [SubtitleTrack(index=2, language="eng", codec="subrip")])


class TestBloatedFilter(unittest.TestCase):
    """The --bloated convenience filter: 4K files, or any heavy-bpp encode."""

    def test_bloated_matches_4k_and_heavy_bpp_but_not_efficient(self):
        fourk = VideoFile(path="/a.mkv", size=1, mtime=0.0, width=3840, height=2160,
                          fps=24.0, vbitrate=1_000_000)
        heavy = VideoFile(path="/b.mkv", size=1, mtime=0.0, width=1920, height=1080,
                          fps=24.0, vbitrate=int(0.20 * 1920 * 1080 * 24))
        efficient = VideoFile(path="/c.mkv", size=1, mtime=0.0, width=1920, height=1080,
                              fps=24.0, vbitrate=int(0.05 * 1920 * 1080 * 24))
        f = Filter(bloated=True)
        self.assertTrue(f.matches(fourk))
        self.assertTrue(f.matches(heavy))
        self.assertFalse(f.matches(efficient))


if __name__ == "__main__":
    unittest.main(verbosity=2)
