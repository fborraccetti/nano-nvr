import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "nano_nvr" / "app"))

import nano_nvr as nvr  # noqa: E402

TODAY = date(2026, 10, 11)


def settings(root: Path, **kw) -> nvr.Settings:
    cams = kw.pop("cameras", [nvr.Camera(name="front", url="rtsp://cam")])
    kw.setdefault("min_free_gb", 0)
    return nvr.Settings(storage_path=root, cameras=cams, **kw)


def touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    return path


class UrlTest(unittest.TestCase):
    def test_credentials_are_percent_encoded(self):
        url = nvr.with_credentials("rtsp://192.0.2.10:554/ch1", "admin", "pa@ss:w/rd")
        self.assertEqual(url, "rtsp://admin:pa%40ss%3Aw%2Frd@192.0.2.10:554/ch1")

    def test_credentials_replace_existing_ones(self):
        url = nvr.with_credentials("rtsp://old:old@cam:554", "admin", "new")
        self.assertEqual(url, "rtsp://admin:new@cam:554")

    def test_no_username_keeps_url(self):
        self.assertEqual(nvr.with_credentials("rtsp://u:p@cam", "", "x"), "rtsp://u:p@cam")

    def test_redact(self):
        self.assertEqual(nvr.redact("rtsp://admin:secret@cam:554/x"), "rtsp://***@cam:554/x")
        self.assertEqual(nvr.redact("rtsp://cam:554/x"), "rtsp://cam:554/x")


class SettingsTest(unittest.TestCase):
    def load(self, raw: dict) -> nvr.Settings:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(raw, f)
        self.addCleanup(Path(f.name).unlink)
        return nvr.load_settings(Path(f.name))

    def test_defaults_and_disabled_cameras(self):
        s = self.load({"cameras": [
            {"name": "a", "url": "rtsp://a", "username": "u", "password": "p"},
            {"name": "b", "url": "rtsp://b", "enabled": False},
        ]})
        self.assertEqual(s.storage_path, Path("/media/nano_nvr"))
        self.assertEqual(s.retention_days, 30)
        self.assertEqual([c.name for c in s.cameras], ["a"])
        self.assertEqual(s.cameras[0].url, "rtsp://u:p@a")
        self.assertEqual(s.cameras[0].mode, "snapshot")
        self.assertEqual(s.cameras[0].interval, 60)

    def test_snapshot_rate(self):
        s = self.load({"cameras": [
            {"name": "a", "url": "rtsp://a", "snapshots_per_minute": 6},
            {"name": "b", "url": "rtsp://b", "snapshots_per_minute": 0.5},
        ]})
        self.assertEqual([c.interval for c in s.cameras], [10, 120])

    def test_overrides(self):
        s = self.load({"retention_days": 7, "segment_minutes": 5, "cameras": [
            {"name": "a", "url": "rtsp://a", "mode": "record", "fps": 5, "audio": True},
        ]})
        self.assertEqual(s.retention_days, 7)
        self.assertEqual(s.segment_minutes, 5)
        self.assertEqual((s.cameras[0].mode, s.cameras[0].fps, s.cameras[0].audio), ("record", 5, True))

    def test_live_url_gets_credentials(self):
        s = self.load({"cameras": [
            {"name": "a", "url": "rtsp://cam/main", "live_url": "rtsp://cam/sub", "username": "u", "password": "p@"},
            {"name": "b", "url": "rtsp://cam/main"},
        ]})
        self.assertEqual(s.cameras[0].live_url, "rtsp://u:p%40@cam/sub")
        self.assertEqual(s.cameras[1].live_url, "")
        cmd = nvr.live_cmd(s, s.cameras[0])
        self.assertEqual(cmd[cmd.index("-i") + 1], "rtsp://u:p%40@cam/sub")
        self.assertEqual(nvr.live_cmd(s, s.cameras[1])[nvr.live_cmd(s, s.cameras[1]).index("-i") + 1], "rtsp://cam/main")

    def test_duplicate_names_rejected(self):
        with self.assertRaises(ValueError):
            self.load({"cameras": [{"name": "a", "url": "rtsp://a"}, {"name": "a", "url": "rtsp://b"}]})


class RecordCmdTest(unittest.TestCase):
    def setUp(self):
        self.s = settings(Path("/media/nvr"), segment_minutes=10)

    def test_stream_copy_by_default(self):
        cmd = nvr.record_cmd(self.s, nvr.Camera(name="c", url="rtsp://c", mode="record"))
        self.assertIn("copy", cmd)
        self.assertIn("-an", cmd)
        self.assertNotIn("libx264", cmd)
        self.assertEqual(cmd[cmd.index("-segment_time") + 1], "600")
        self.assertEqual(cmd[-1], "/media/nvr/c/%Y-%m-%d/%H%M%S.mp4")

    def test_hevc_gets_hvc1_tag(self):
        cmd = nvr.record_cmd(self.s, nvr.Camera(name="c", url="rtsp://c", mode="record"), "hevc")
        self.assertEqual(cmd[cmd.index("-tag:v") + 1], "hvc1")
        self.assertNotIn("-tag:v", nvr.record_cmd(self.s, nvr.Camera(name="c", url="rtsp://c"), "h264"))

    def test_fps_reencodes(self):
        cmd = nvr.record_cmd(self.s, nvr.Camera(name="c", url="rtsp://c", mode="record", fps=5, audio=True))
        self.assertIn("fps=5", cmd)
        self.assertIn("libx264", cmd)
        self.assertNotIn("-tag:v", cmd)
        self.assertIn("aac", cmd)


class HousekeepingTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.cam = self.root / "front"

    def run_housekeeping(self, s, build_ok=True):
        def fake_build(s, day_dir, frames, video):
            if build_ok:
                video.write_bytes(b"mp4")
            return build_ok
        with mock.patch.object(nvr, "build_timelapse", side_effect=fake_build) as build:
            nvr.housekeeping(s, today=TODAY)
        return build

    def test_finished_day_becomes_timelapse_and_frames_are_removed(self):
        touch(self.cam / "2026-10-10" / "120000.jpg")
        touch(self.cam / "2026-10-11" / "120000.jpg")
        build = self.run_housekeeping(settings(self.root))
        self.assertEqual(build.call_count, 1)
        self.assertTrue((self.cam / "2026-10-10.mp4").exists())
        self.assertFalse((self.cam / "2026-10-10").exists())
        self.assertTrue((self.cam / "2026-10-11" / "120000.jpg").exists(), "today is untouched")

    def test_keep_snapshots(self):
        touch(self.cam / "2026-10-10" / "120000.jpg")
        self.run_housekeeping(settings(self.root, keep_snapshots=True))
        self.assertTrue((self.cam / "2026-10-10" / "120000.jpg").exists())
        self.assertTrue((self.cam / "2026-10-10.mp4").exists())

    def test_failed_timelapse_keeps_frames(self):
        touch(self.cam / "2026-10-10" / "120000.jpg")
        self.run_housekeeping(settings(self.root), build_ok=False)
        self.assertTrue((self.cam / "2026-10-10" / "120000.jpg").exists())

    def test_existing_timelapse_is_not_rebuilt_when_keeping_snapshots(self):
        touch(self.cam / "2026-10-10" / "120000.jpg")
        touch(self.cam / "2026-10-10.mp4")
        build = self.run_housekeeping(settings(self.root, keep_snapshots=True))
        build.assert_not_called()

    def test_frames_left_after_compaction_are_appended(self):
        touch(self.cam / "2026-10-10" / "235900.jpg")
        touch(self.cam / "2026-10-10.mp4")
        with mock.patch.object(nvr, "concat_videos", return_value=True) as concat:
            build = self.run_housekeeping(settings(self.root))
        part = self.cam / ".part-2026-10-10.mp4"
        self.assertEqual(build.call_args.args[3], part)
        concat.assert_called_once_with(mock.ANY, [self.cam / "2026-10-10.mp4", part], self.cam / "2026-10-10.mp4")
        self.assertFalse(part.exists())
        self.assertFalse((self.cam / "2026-10-10").exists())

    def test_failed_append_keeps_frames(self):
        touch(self.cam / "2026-10-10" / "235900.jpg")
        touch(self.cam / "2026-10-10.mp4")
        with mock.patch.object(nvr, "concat_videos", return_value=False):
            self.run_housekeeping(settings(self.root))
        self.assertTrue((self.cam / "2026-10-10" / "235900.jpg").exists())
        self.assertFalse((self.cam / "2026-10-10" / ".timelapse").exists())

    def test_recorded_segments_are_kept(self):
        touch(self.cam / "2026-10-10" / "120000.mp4")
        build = self.run_housekeeping(settings(self.root))
        build.assert_not_called()
        self.assertTrue((self.cam / "2026-10-10" / "120000.mp4").exists())

    def test_retention(self):
        touch(self.cam / "2026-09-10.mp4")
        touch(self.cam / "2026-09-11" / "000000.mp4")
        touch(self.cam / "2026-09-12.mp4")
        touch(self.cam / "notes.txt")
        self.run_housekeeping(settings(self.root, retention_days=30))
        self.assertFalse((self.cam / "2026-09-10.mp4").exists())
        self.assertFalse((self.cam / "2026-09-11").exists())
        self.assertTrue((self.cam / "2026-09-12.mp4").exists())
        self.assertTrue((self.cam / "notes.txt").exists())

    def test_retention_zero_keeps_everything(self):
        touch(self.cam / "2020-01-01.mp4")
        self.run_housekeeping(settings(self.root, retention_days=0))
        self.assertTrue((self.cam / "2020-01-01.mp4").exists())

    def test_record_mode_prepares_day_folders(self):
        cam = nvr.Camera(name="front", url="rtsp://c", mode="record")
        with mock.patch.object(nvr, "date", wraps=date) as fake_date:
            fake_date.today.return_value = TODAY
            nvr.ensure_day_dirs(settings(self.root, cameras=[cam]), cam)
        self.assertTrue((self.cam / "2026-10-11").is_dir())
        self.assertTrue((self.cam / "2026-10-12").is_dir())


class FreeSpaceTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_oldest_days_removed_until_enough_space(self):
        old = touch(self.root / "a" / "2026-10-01.mp4")
        mid = touch(self.root / "b" / "2026-10-02" / "000000.mp4").parent
        new = touch(self.root / "a" / "2026-10-03.mp4")
        today = touch(self.root / "a" / "2026-10-11" / "000000.mp4").parent
        free = iter([1, 1, 1, 10])
        with mock.patch.object(nvr, "free_gb", side_effect=lambda _: next(free)), \
                mock.patch.object(nvr, "build_timelapse") as build:
            nvr.enforce_free_space(settings(self.root, min_free_gb=5), TODAY)
        build.assert_not_called()  # no frames today, nothing to compact
        self.assertFalse(old.exists())
        self.assertFalse(mid.exists())
        self.assertTrue(new.exists())
        self.assertTrue(today.exists())

    def compact(self, build_ok=True, **kw):
        def fake_build(s, frames_dir, frames, video):
            self.built = (frames_dir, frames, video)
            touch(self.root / "front" / "2026-10-11" / "121000.jpg")  # taken while encoding
            if build_ok:
                video.write_bytes(b"mp4")
            return build_ok
        with mock.patch.object(nvr, "free_gb", return_value=1), \
                mock.patch.object(nvr, "build_timelapse", side_effect=fake_build), \
                self.assertLogs(nvr.log, "WARNING"):
            nvr.enforce_free_space(settings(self.root, min_free_gb=5, **kw), TODAY)

    def test_low_space_compacts_todays_frames(self):
        day = self.root / "front" / "2026-10-11"
        touch(day / "120000.jpg")
        touch(day / "120500.jpg")
        self.compact()
        self.assertEqual(self.built, (day / ".timelapse", 2, self.root / "front" / "2026-10-11.mp4"))
        self.assertTrue((self.root / "front" / "2026-10-11.mp4").exists())
        self.assertEqual(sorted(f.name for f in day.iterdir()), ["121000.jpg"],
                         "frames taken meanwhile are kept for the next round")

    def test_failed_compaction_keeps_frames(self):
        day = self.root / "front" / "2026-10-11"
        touch(day / "120000.jpg")
        self.compact(build_ok=False)
        self.assertEqual(sorted(f.name for f in day.iterdir()), ["120000.jpg", "121000.jpg"])

    def test_no_compaction_when_keeping_snapshots(self):
        touch(self.root / "front" / "2026-10-11" / "120000.jpg")
        with mock.patch.object(nvr, "free_gb", return_value=1), \
                mock.patch.object(nvr, "build_timelapse") as build, self.assertLogs(nvr.log, "ERROR"):
            nvr.enforce_free_space(settings(self.root, min_free_gb=5, keep_snapshots=True), TODAY)
        build.assert_not_called()

    def test_today_is_never_removed(self):
        today = touch(self.root / "a" / "2026-10-11" / "000000.mp4")
        with mock.patch.object(nvr, "free_gb", return_value=0):
            nvr.enforce_free_space(settings(self.root, min_free_gb=5), TODAY)
        self.assertTrue(today.exists())


class SnapshotLoopTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.cam = nvr.Camera(name="front", url="rtsp://cam")
        self.addCleanup(nvr.errors.clear)

    def run_loop(self, s, grab=None, rounds=3):
        """Run the loop for a few rounds; returns the snapshots it tried to take."""
        stop = threading.Event()
        waits = []

        def fake_wait(_timeout):
            waits.append(_timeout)
            if len(waits) >= rounds:
                stop.set()

        with mock.patch.object(nvr, "grab_snapshot", side_effect=grab) as fake_grab, \
                mock.patch.object(stop, "wait", side_effect=fake_wait):
            nvr.snapshot_loop(s, self.cam, stop)
        return fake_grab.call_count

    def test_errors_do_not_end_the_thread(self):
        disk_full = OSError(28, "No space left on device")
        with self.assertLogs(nvr.log, "ERROR"):
            self.assertEqual(self.run_loop(settings(self.root), grab=disk_full), 3)
        self.assertIn("No space left", nvr.errors["front"][1])

    def test_paused_when_disk_almost_full(self):
        with mock.patch.object(nvr, "free_gb", return_value=2), self.assertLogs(nvr.log, "ERROR") as logs:
            self.assertEqual(self.run_loop(settings(self.root, min_free_gb=5)), 0)
        self.assertEqual(len(logs.records), 1, "logged once, not at every round")
        self.assertIn("paused", nvr.errors["front"][1])

    def test_not_paused_above_half_threshold(self):
        with mock.patch.object(nvr, "free_gb", return_value=3):
            self.assertEqual(self.run_loop(settings(self.root, min_free_gb=5)), 3)

    def test_paused_while_storage_not_mounted(self):
        with mock.patch.object(nvr, "storage_missing", return_value=True), \
                self.assertLogs(nvr.log, "ERROR") as logs:
            self.assertEqual(self.run_loop(settings(self.root, require_mount=True)), 0)
        self.assertEqual(len(logs.records), 1, "logged once, not at every round")
        self.assertIn("not mounted", nvr.errors["front"][1])


class MountTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.addCleanup(nvr.errors.clear)

    def test_mount_point_is_first_folder_under_media(self):
        self.assertEqual(nvr.mount_point(Path("/media/nvr/cams")), Path("/media/nvr"))
        self.assertEqual(nvr.mount_point(Path("/share/nvr")), Path("/share/nvr"))

    def test_only_checked_when_required(self):
        with mock.patch.object(nvr.os.path, "ismount", return_value=False):
            self.assertFalse(nvr.storage_missing(settings(self.root)))
            self.assertTrue(nvr.storage_missing(settings(self.root, require_mount=True)))
        with mock.patch.object(nvr.os.path, "ismount", return_value=True):
            self.assertFalse(nvr.storage_missing(settings(self.root, require_mount=True)))

    def test_recorder_waits_without_creating_folders(self):
        s = settings(self.root, require_mount=True,
                     cameras=[nvr.Camera(name="gate", url="rtsp://cam", mode="record")])
        stop = threading.Event()
        with mock.patch.object(nvr, "storage_missing", return_value=True), \
                mock.patch.object(nvr.subprocess, "Popen") as popen, \
                mock.patch.object(stop, "wait", side_effect=lambda _t: stop.set()), \
                self.assertLogs(nvr.log, "ERROR"):
            nvr.record_loop(s, s.cameras[0], stop, {})
        popen.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_housekeeping_skipped_while_unmounted(self):
        s = settings(self.root, require_mount=True, retention_days=1)
        old = touch(self.root / "front" / "2026-01-01.mp4")
        with mock.patch.object(nvr, "storage_missing", return_value=True):
            nvr.housekeeping(s, today=TODAY)
        self.assertTrue(old.exists())

    def test_status_while_unmounted(self):
        with mock.patch.object(nvr, "storage_missing", return_value=True):
            st = nvr.status(settings(self.root, require_mount=True))
        self.assertTrue(st["storage_missing"])
        self.assertEqual(st["disk"], {"total": 0, "used": 0, "free": 0})


class CameraStorageTest(unittest.TestCase):
    def test_counts_days_sizes_and_projection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "front").mkdir()
            (root / "front" / "2026-10-09.mp4").write_bytes(b"x" * 100)
            (root / "front" / "2026-10-10.mp4").write_bytes(b"x" * 300)
            touch(root / "front" / "2026-10-11" / "120000.jpg")
            touch(root / "front" / "2026-10-11" / ".120100.jpg")  # snapshot being written
            (root / "front" / "2026-10-12").mkdir()  # pre-created empty folder
            s = settings(root, retention_days=10)
            st = nvr.camera_storage(s, s.cameras[0], TODAY)
        self.assertEqual(st["days"], 3)
        self.assertEqual(st["bytes"], 401)
        self.assertEqual((st["oldest"], st["newest"]), ("2026-10-09", "2026-10-11"))
        self.assertEqual(st["timelapses"], 2)
        self.assertEqual(st["avg_day_bytes"], 200)
        self.assertEqual(st["projected_bytes"], 2000)
        self.assertIsNotNone(st["last_file_at"])

    def test_missing_folder(self):
        s = settings(Path("/nonexistent"))
        st = nvr.camera_storage(s, s.cameras[0], TODAY)
        self.assertEqual((st["days"], st["bytes"], st["avg_day_bytes"]), (0, 0, None))


class PanelTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        touch(root / "front" / date.today().isoformat() / "120000.jpg")
        nvr.PanelHandler.settings = settings(root, cameras=[nvr.Camera(name="front", url="rtsp://u:p@cam")])
        cls.server = nvr.ThreadingHTTPServer(("127.0.0.1", 0), nvr.PanelHandler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.tmp.cleanup()

    def get(self, path):
        with urllib.request.urlopen(self.base + path) as res:
            return res.status, res.headers["Content-Type"], res.read()

    def test_page(self):
        status, ctype, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", ctype)
        self.assertIn(b"api/status", body)

    def test_status_redacts_credentials(self):
        _, ctype, body = self.get("/api/status")
        st = json.loads(body)
        self.assertEqual(ctype, "application/json")
        self.assertEqual(st["cameras"][0]["url"], "rtsp://***@cam")
        self.assertNotIn(b"u:p@", body)
        self.assertEqual(st["cameras"][0]["storage"]["days"], 1)

    def test_preview_uses_latest_snapshot(self):
        status, ctype, body = self.get("/preview/front.jpg")
        self.assertEqual((status, ctype, body), (200, "image/jpeg", b"x"))

    def fake_ffmpeg(self, script):
        exe = Path(self.tmp.name) / "fake-ffmpeg"
        exe.write_text("#!/bin/sh\n" + script)
        exe.chmod(0o755)
        return mock.patch.object(nvr.PanelHandler.settings, "ffmpeg", str(exe))

    def live(self, after=0):
        nvr.live_sessions.clear()
        with urllib.request.urlopen(f"{self.base}/live/front.jpg?after={after}") as res:
            return res.headers["Content-Type"], res.headers["X-Frame-Seq"], res.read()

    def test_live_returns_latest_frame(self):
        # Two JPEGs back to back, as ffmpeg's image2pipe writes them.
        with self.fake_ffmpeg("printf '\\377\\330AA\\377\\331\\377\\330BB\\377\\331'\n"):
            ctype, seq, body = self.live()
        self.assertEqual(ctype, "image/jpeg")
        self.assertIn(body, (b"\xff\xd8AA\xff\xd9", b"\xff\xd8BB\xff\xd9"))
        self.assertGreaterEqual(int(seq), 1)

    def test_live_dead_camera(self):
        with self.fake_ffmpeg("echo 'Connection refused' >&2; exit 1\n"), self.assertRaises(urllib.error.HTTPError) as ctx:
            self.live()
        self.assertEqual(ctx.exception.code, 502)

    def test_live_cmd(self):
        cmd = nvr.live_cmd(settings(Path("/x")), nvr.Camera(name="front", url="rtsp://cam"))
        self.assertIn("rtsp://cam", cmd)
        self.assertEqual(cmd[cmd.index("-f") + 1], "image2pipe")
        self.assertIn(f"fps={nvr.LIVE_FPS}", cmd[cmd.index("-vf") + 1])


    def test_unknown_paths(self):
        for path in ("/preview/other.jpg", "/preview/../options.jpg", "/live/other.jpg", "/nope"):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self.get(path)
            self.assertEqual(ctx.exception.code, 404, path)

    def test_only_ingress_proxy_allowed(self):
        with mock.patch.object(nvr, "PANEL_CLIENTS", {"172.30.32.2"}), mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NANO_NVR_PANEL_OPEN", None)
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self.get("/api/status")
        self.assertEqual(ctx.exception.code, 403)


class LiveSessionTest(unittest.TestCase):
    def test_jpeg_frames_split_across_chunks(self):
        import io
        data = b"junk\xff\xd8one\xff\xd9\xff\xd8two\xff\xd9\xff\xd8par"
        stream = io.BufferedReader(io.BytesIO(data), buffer_size=3)
        self.assertEqual(list(nvr.jpeg_frames(stream)), [b"\xff\xd8one\xff\xd9", b"\xff\xd8two\xff\xd9"])

    def test_counter_from_an_ended_session_does_not_stall(self):
        session = object.__new__(nvr.LiveSession)
        session.cond = threading.Condition()
        session.frame, session.seq, session.done = b"jpg", 3, False
        start = time.monotonic()
        self.assertEqual(session.next_frame(after=500), (3, b"jpg"))
        self.assertLess(time.monotonic() - start, 1)


class DayOfTest(unittest.TestCase):
    def test_names(self):
        self.assertEqual(nvr.day_of(Path("2026-10-10")), date(2026, 10, 10))
        self.assertEqual(nvr.day_of(Path("2026-10-10.mp4")), date(2026, 10, 10))
        self.assertIsNone(nvr.day_of(Path(".2026-10-10.mp4")))
        self.assertIsNone(nvr.day_of(Path("readme.txt")))


if __name__ == "__main__":
    unittest.main()
