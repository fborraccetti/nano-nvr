#!/usr/bin/env python3
"""Nano NVR - lightweight RTSP camera recorder for Home Assistant.

Each camera runs in one of two modes:

* snapshot: grab one keyframe every `interval` seconds. Once a day is over its
  frames become a timelapse MP4 and, unless `keep_snapshots` is set, the JPGs
  are deleted. Nothing is decoded between two shots, so the cost is close to 0.
* record: copy the RTSP stream to disk as MP4 segments of `segment_minutes`.
  The video is not re-encoded unless the camera sets `fps`.

Layout under `storage_path`:

    <camera>/<YYYY-MM-DD>/<HHMMSS>.jpg   snapshots of a day
    <camera>/<YYYY-MM-DD>/<HHMMSS>.mp4   recorded segments of a day
    <camera>/<YYYY-MM-DD>.mp4            timelapse of a finished day

Days older than `retention_days` are deleted, and the oldest days go first
whenever free space drops under `min_free_gb`.

A small status page (summary, camera previews, storage per camera) is served
on the Home Assistant ingress port and shows up as a sidebar panel.
"""

import argparse
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, fields
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

log = logging.getLogger("nano_nvr")

DEFAULT_OPTIONS = Path("/data/options.json")
# RTSP socket timeout in microseconds: ffmpeg exits instead of hanging on a dead camera.
RTSP_TIMEOUT_US = "10000000"
HOUSEKEEPING_EVERY = 600
VERSION = os.environ.get("NANO_NVR_VERSION", "dev")
STARTED = time.time()

# Last error per camera, shown in the panel: name -> (timestamp, message).
errors: dict = {}


@dataclass
class Camera:
    name: str
    url: str
    mode: str = "snapshot"
    interval: int = 60
    fps: int = 0
    audio: bool = False


@dataclass
class Settings:
    storage_path: Path
    cameras: list
    retention_days: int = 30
    min_free_gb: float = 5.0
    timelapse_fps: int = 24
    timelapse_quality: int = 28
    timelapse_height: int = 0
    keep_snapshots: bool = False
    segment_minutes: int = 10
    snapshot_timeout: int = 20
    log_level: str = "info"
    ffmpeg: str = "ffmpeg"


def with_credentials(url: str, username: str, password: str) -> str:
    """Put username/password into the URL, percent-encoding special characters."""
    if not username:
        return url
    parts = urlsplit(url)
    host = parts.netloc.rsplit("@", 1)[-1]
    auth = quote(username, safe="")
    if password:
        auth += ":" + quote(password, safe="")
    return urlunsplit(parts._replace(netloc=f"{auth}@{host}"))


def redact(url: str) -> str:
    parts = urlsplit(url)
    if "@" not in parts.netloc:
        return url
    return urlunsplit(parts._replace(netloc="***@" + parts.netloc.rsplit("@", 1)[-1]))


def load_settings(path: Path) -> Settings:
    raw = json.loads(path.read_text())
    cameras = []
    for c in raw.get("cameras", []):
        if not c.get("enabled", True):
            continue
        if any(cam.name == c["name"] for cam in cameras):
            raise ValueError(f"duplicate camera name: {c['name']}")
        cameras.append(Camera(
            name=c["name"],
            url=with_credentials(c["url"], c.get("username", ""), c.get("password", "")),
            mode=c.get("mode", "snapshot"),
            interval=int(c.get("interval", 60)),
            fps=int(c.get("fps", 0)),
            audio=bool(c.get("audio", False)),
        ))
    tunables = {f.name for f in fields(Settings)} - {"storage_path", "cameras", "ffmpeg"}
    return Settings(
        storage_path=Path(raw.get("storage_path", "/media/nano_nvr")),
        cameras=cameras,
        ffmpeg=os.environ.get("FFMPEG", "ffmpeg"),
        **{k: raw[k] for k in tunables if k in raw},
    )


def day_of(entry: Path):
    """Date encoded in a day folder or timelapse name, None for anything else."""
    try:
        return date.fromisoformat(entry.name.removesuffix(".mp4"))
    except ValueError:
        return None


def remove(entry: Path) -> None:
    if entry.is_dir():
        shutil.rmtree(entry, ignore_errors=True)
    else:
        entry.unlink(missing_ok=True)


def last_line(stderr: bytes) -> str:
    lines = stderr.decode(errors="replace").strip().splitlines()
    return lines[-1] if lines else "no output"


def lower_priority() -> None:
    os.nice(19)


# --- snapshot mode ----------------------------------------------------------

def grab_snapshot(s: Settings, cam: Camera, dest: Path) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Leading dot: never matched by the "[0-9]*.jpg" glob used for timelapses.
    tmp = dest.with_name("." + dest.name)
    cmd = [
        s.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-rtsp_transport", "tcp", "-timeout", RTSP_TIMEOUT_US,
        "-skip_frame", "nokey", "-i", cam.url,
        "-frames:v", "1", "-q:v", "2", "-update", "1", str(tmp),
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, timeout=s.snapshot_timeout)
    except subprocess.TimeoutExpired:
        tmp.unlink(missing_ok=True)
        log.warning("%s: snapshot timed out after %ss", cam.name, s.snapshot_timeout)
        errors[cam.name] = (time.time(), f"snapshot timed out after {s.snapshot_timeout}s")
        return False
    if res.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        log.warning("%s: snapshot failed: %s", cam.name, last_line(res.stderr))
        errors[cam.name] = (time.time(), "snapshot failed: " + last_line(res.stderr))
        return False
    tmp.rename(dest)
    errors.pop(cam.name, None)
    log.debug("%s: saved %s", cam.name, dest)
    return True


def snapshot_loop(s: Settings, cam: Camera, stop: threading.Event) -> None:
    while not stop.is_set():
        started = time.monotonic()
        now = datetime.now()
        grab_snapshot(s, cam, s.storage_path / cam.name / now.date().isoformat() / f"{now:%H%M%S}.jpg")
        stop.wait(max(0, cam.interval - (time.monotonic() - started)))


def build_timelapse(s: Settings, day_dir: Path, frames: int, video: Path) -> bool:
    tmp = video.with_name("." + video.name)
    vf = f"scale=-2:{s.timelapse_height}" if s.timelapse_height else "null"
    cmd = [
        s.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-framerate", str(s.timelapse_fps),
        "-pattern_type", "glob", "-i", str(day_dir / "[0-9]*.jpg"),
        "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", str(s.timelapse_quality),
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-f", "mp4", str(tmp),
    ]
    log.info("%s: building %s from %d frames", video.parent.name, video.name, frames)
    res = subprocess.run(cmd, capture_output=True, preexec_fn=lower_priority)
    if res.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        log.error("%s: timelapse %s failed: %s", video.parent.name, video.name, last_line(res.stderr))
        return False
    tmp.rename(video)
    return True


# --- record mode ------------------------------------------------------------

def probe_codec(s: Settings, cam: Camera) -> str:
    """Video codec of the stream (e.g. "h264", "hevc"), empty when unknown."""
    cmd = [s.ffmpeg, "-hide_banner", "-nostdin", "-rtsp_transport", "tcp",
           "-timeout", RTSP_TIMEOUT_US, "-i", cam.url]
    try:
        res = subprocess.run(cmd, capture_output=True, timeout=s.snapshot_timeout)
    except subprocess.TimeoutExpired:
        return ""
    match = re.search(r"Video: (\w+)", res.stderr.decode(errors="replace"))
    return match.group(1) if match else ""


def record_cmd(s: Settings, cam: Camera, codec: str = "") -> list:
    cmd = [
        s.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
        "-rtsp_transport", "tcp", "-timeout", RTSP_TIMEOUT_US, "-i", cam.url,
        "-map", "0:v:0",
    ]
    if cam.fps:
        cmd += ["-vf", f"fps={cam.fps}", "-c:v", "libx264", "-preset", "veryfast",
                "-crf", "26", "-g", str(cam.fps * 2)]
    else:
        cmd += ["-c:v", "copy"]
        if codec == "hevc":
            # hvc1 instead of ffmpeg's default hev1: required by Safari/iOS to play H.265.
            cmd += ["-tag:v", "hvc1"]
    if cam.audio:
        cmd += ["-map", "0:a:0?", "-c:a", "aac", "-b:a", "64k"]
    else:
        cmd += ["-an"]
    cmd += [
        "-f", "segment", "-segment_time", str(s.segment_minutes * 60),
        "-segment_atclocktime", "1", "-reset_timestamps", "1", "-strftime", "1",
        "-segment_format", "mp4",
        # Fragmented MP4: a segment cut short by a crash or restart is still playable.
        "-segment_format_options", "movflags=+frag_keyframe+empty_moov+default_base_moof",
        str(s.storage_path / cam.name / "%Y-%m-%d" / "%H%M%S.mp4"),
    ]
    return cmd


def ensure_day_dirs(s: Settings, cam: Camera) -> None:
    """The segment muxer cannot create folders: keep today's and tomorrow's ready."""
    today = date.today()
    for d in (today, today + timedelta(days=1)):
        (s.storage_path / cam.name / d.isoformat()).mkdir(parents=True, exist_ok=True)


def record_loop(s: Settings, cam: Camera, stop: threading.Event, procs: dict) -> None:
    backoff = 5
    while not stop.is_set():
        ensure_day_dirs(s, cam)
        codec = "" if cam.fps else probe_codec(s, cam)
        started = time.monotonic()
        proc = subprocess.Popen(record_cmd(s, cam, codec), stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                preexec_fn=lower_priority if cam.fps else None)
        procs[cam.name] = proc
        log.info("%s: recording started%s", cam.name, f" ({codec})" if codec else "")
        _, err = proc.communicate()
        if stop.is_set():
            break
        log.warning("%s: recorder exited (%s): %s", cam.name, proc.returncode, last_line(err))
        errors[cam.name] = (time.time(), "recorder exited: " + last_line(err))
        backoff = 5 if time.monotonic() - started > 120 else min(backoff * 2, 300)
        stop.wait(backoff)


# --- housekeeping -----------------------------------------------------------

def free_gb(path: Path) -> float:
    return shutil.disk_usage(path).free / 1e9


def housekeeping(s: Settings, today=None) -> None:
    today = today or date.today()
    oldest_kept = today - timedelta(days=s.retention_days - 1)  # today counts as one
    for cam in s.cameras:
        if cam.mode == "record":
            ensure_day_dirs(s, cam)
        cam_dir = s.storage_path / cam.name
        if not cam_dir.is_dir():
            continue
        for entry in sorted(cam_dir.iterdir()):
            day = day_of(entry)
            if day is None:
                continue
            if s.retention_days and day < oldest_kept:
                remove(entry)
                log.info("%s: removed %s (retention)", cam.name, entry.name)
                continue
            if not entry.is_dir() or day >= today:
                continue
            frames = sorted(entry.glob("[0-9]*.jpg"))
            if frames:
                video = entry.with_suffix(".mp4")
                if (video.exists() or build_timelapse(s, entry, len(frames), video)) and not s.keep_snapshots:
                    for f in frames:
                        f.unlink(missing_ok=True)
            try:
                entry.rmdir()  # only succeeds when nothing is left in the folder
            except OSError:
                pass
    enforce_free_space(s, today)


def enforce_free_space(s: Settings, today: date) -> None:
    if not s.min_free_gb or not s.storage_path.is_dir():
        return
    while free_gb(s.storage_path) < s.min_free_gb:
        past = [
            (day, entry)
            for cam_dir in s.storage_path.iterdir() if cam_dir.is_dir()
            for entry in cam_dir.iterdir()
            if (day := day_of(entry)) is not None and day < today
        ]
        if not past:
            log.error("Free space under %s GB and only today's files left", s.min_free_gb)
            return
        day, entry = min(past)
        remove(entry)
        log.warning("%s: removed %s, free space under %s GB", entry.parent.name, entry.name, s.min_free_gb)


def housekeeping_loop(s: Settings, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            housekeeping(s)
        except Exception:
            log.exception("Housekeeping failed")
        stop.wait(HOUSEKEEPING_EVERY)


# --- status panel (Home Assistant ingress) ---------------------------------

PANEL_HTML = Path(__file__).with_name("panel.html")
PANEL_PORT = int(os.environ.get("NANO_NVR_PANEL_PORT", "8099"))
# Only the Supervisor ingress proxy may talk to the panel (plus localhost for tests).
PANEL_CLIENTS = {"172.30.32.2", "127.0.0.1", "::1"}
PREVIEW_DIR = Path(os.environ.get("NANO_NVR_PREVIEW_DIR", "/tmp/nano_nvr_preview"))
PREVIEW_MAX_AGE = 60
# Live view: MJPEG re-encoded by ffmpeg only while someone watches, capped in
# frame rate, height and duration so a forgotten tab cannot burn CPU forever.
STREAM_FPS = 10
STREAM_HEIGHT = 720
STREAM_MAX_SECONDS = 1800
preview_locks = defaultdict(threading.Lock)


def camera_storage(s: Settings, cam: Camera, today: date) -> dict:
    cam_dir = s.storage_path / cam.name
    days, timelapses, total, finished, last_file = set(), 0, 0, 0, 0.0
    if cam_dir.is_dir():
        for entry in cam_dir.iterdir():
            day = day_of(entry)
            if day is None:
                continue
            if entry.is_dir():
                files = [f.stat() for f in entry.iterdir() if f.is_file() and not f.name.startswith(".")]
                if not files:
                    continue
                size = sum(f.st_size for f in files)
                if day == today:
                    last_file = max(f.st_mtime for f in files)
            else:
                size = entry.stat().st_size
                timelapses += 1
            days.add(day)
            total += size
            if day < today:
                finished += size
    past_days = len([d for d in days if d < today])
    avg = finished / past_days if past_days else None
    return {
        "bytes": total,
        "days": len(days),
        "oldest": min(days).isoformat() if days else None,
        "newest": max(days).isoformat() if days else None,
        "timelapses": timelapses,
        "avg_day_bytes": avg,
        "projected_bytes": avg * s.retention_days if avg and s.retention_days else None,
        "last_file_at": last_file or None,
    }


def status(s: Settings) -> dict:
    today = date.today()
    disk = shutil.disk_usage(s.storage_path)
    cameras = []
    for cam in s.cameras:
        err = errors.get(cam.name)
        cameras.append({
            "name": cam.name,
            "mode": cam.mode,
            "interval": cam.interval,
            "fps": cam.fps,
            "audio": cam.audio,
            "url": redact(cam.url),
            "storage": camera_storage(s, cam, today),
            "error": {"at": err[0], "message": err[1]} if err else None,
        })
    return {
        "version": VERSION,
        "started_at": STARTED,
        "now": time.time(),
        "storage_path": str(s.storage_path),
        "retention_days": s.retention_days,
        "min_free_gb": s.min_free_gb,
        "segment_minutes": s.segment_minutes,
        "timelapse": {"fps": s.timelapse_fps, "quality": s.timelapse_quality,
                      "height": s.timelapse_height, "keep_snapshots": s.keep_snapshots},
        "disk": {"total": disk.total, "used": disk.used, "free": disk.free},
        "nvr_bytes": sum(c["storage"]["bytes"] for c in cameras),
        "cameras": cameras,
    }


def preview(s: Settings, cam: Camera):
    """Latest snapshot when recent enough, otherwise a live frame cached for a minute."""
    if cam.mode == "snapshot":
        frames = sorted((s.storage_path / cam.name / date.today().isoformat()).glob("[0-9]*.jpg"))
        if frames and time.time() - frames[-1].stat().st_mtime < max(2 * cam.interval, 120):
            return frames[-1]
    cached = PREVIEW_DIR / f"{cam.name}.jpg"
    with preview_locks[cam.name]:
        if cached.exists() and time.time() - cached.stat().st_mtime < PREVIEW_MAX_AGE:
            return cached
        grab_snapshot(s, cam, cached)
    return cached if cached.exists() else None


def stream_cmd(s: Settings, cam: Camera) -> list:
    return [
        s.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
        "-fflags", "nobuffer", "-flags", "low_delay",
        "-rtsp_transport", "tcp", "-timeout", RTSP_TIMEOUT_US, "-i", cam.url,
        "-map", "0:v:0", "-an", "-t", str(STREAM_MAX_SECONDS),
        "-vf", f"fps={STREAM_FPS},scale=-2:'min({STREAM_HEIGHT},ih)'",
        "-c:v", "mjpeg", "-q:v", "6", "-f", "mpjpeg", "-boundary_tag", "frame", "pipe:1",
    ]


class PanelHandler(BaseHTTPRequestHandler):
    settings: Settings = None

    def do_GET(self):
        if self.client_address[0] not in PANEL_CLIENTS and not os.environ.get("NANO_NVR_PANEL_OPEN"):
            return self.send_error(403)
        path = urlsplit(self.path).path
        cams = {c.name: c for c in self.settings.cameras}
        if path == "/":
            self.reply(PANEL_HTML.read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/status":
            self.reply(json.dumps(status(self.settings)).encode(), "application/json")
        elif path.startswith("/preview/") and path.endswith(".jpg") and path[9:-4] in cams:
            image = preview(self.settings, cams[path[9:-4]])
            if image is None:
                return self.send_error(404)
            self.reply(image.read_bytes(), "image/jpeg")
        elif path.startswith("/stream/") and path.endswith(".mjpg") and path[8:-5] in cams:
            self.stream(cams[path[8:-5]])
        else:
            self.send_error(404)

    def reply(self, body: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def stream(self, cam: Camera) -> None:
        # stderr to a file: a pipe nobody reads would stall ffmpeg once full.
        stderr = tempfile.TemporaryFile()
        proc = subprocess.Popen(stream_cmd(self.settings, cam), stdout=subprocess.PIPE,
                                stderr=stderr, preexec_fn=lower_priority)
        try:
            # Wait for the first bytes: a dead camera gets a 502, not an empty stream.
            chunk = proc.stdout.read1(65536)
            if not chunk:
                proc.wait()
                stderr.seek(0)
                log.warning("%s: live view failed: %s", cam.name, last_line(stderr.read()))
                return self.send_error(502)
            log.debug("%s: live view started", cam.name)
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace;boundary=frame")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            while chunk:
                self.wfile.write(chunk)
                chunk = proc.stdout.read1(65536)
        except OSError:
            pass  # viewer closed the live view
        finally:
            proc.kill()
            proc.wait()
            proc.stdout.close()
            stderr.close()
            log.debug("%s: live view stopped", cam.name)

    def log_message(self, fmt, *args):
        log.debug("panel: " + fmt, *args)


def start_panel(s: Settings) -> None:
    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    PanelHandler.settings = s
    server = ThreadingHTTPServer(("", PANEL_PORT), PanelHandler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="panel", daemon=True).start()
    log.info("Status panel on port %s", PANEL_PORT)


# --- main -------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Lightweight RTSP camera recorder")
    ap.add_argument("--options", type=Path, default=DEFAULT_OPTIONS)
    args = ap.parse_args(argv)

    s = load_settings(args.options)
    logging.basicConfig(
        level=s.log_level.upper(),
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    if not s.cameras:
        log.error("No cameras configured: add at least one in the add-on configuration")
        return 1
    s.storage_path.mkdir(parents=True, exist_ok=True)

    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())

    procs = {}
    threads = [threading.Thread(target=housekeeping_loop, args=(s, stop), name="housekeeping", daemon=True)]
    for cam in s.cameras:
        if cam.mode == "record":
            target, extra = record_loop, (procs,)
            detail = f"segments of {s.segment_minutes} min, " + (f"{cam.fps} fps" if cam.fps else "stream copy")
        else:
            target, extra = snapshot_loop, ()
            detail = f"every {cam.interval}s"
        log.info("%s: %s mode, %s, %s", cam.name, cam.mode, detail, redact(cam.url))
        threads.append(threading.Thread(target=target, args=(s, cam, stop, *extra), name=cam.name, daemon=True))
    for t in threads:
        t.start()
    start_panel(s)
    log.info("Saving to %s, retention %s days, min free %s GB", s.storage_path, s.retention_days, s.min_free_gb)

    while not stop.wait(1):
        pass
    log.info("Stopping")
    for proc in procs.values():
        if proc.poll() is None:
            proc.terminate()
    # ffmpeg may not stop on the first SIGTERM while reading RTSP. The MP4 files
    # are fragmented, so killing it loses at most the last few seconds.
    for proc in procs.values():
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
