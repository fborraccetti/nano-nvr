#!/usr/bin/env python3
"""Nano NVR - lightweight RTSP camera recorder for Home Assistant.

Each camera runs in one of two modes:

* snapshot: grab `snapshots_per_minute` keyframes per minute. Once a day is over its
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
from urllib.parse import parse_qs, quote, urlsplit, urlunsplit

log = logging.getLogger("nano_nvr")

DEFAULT_OPTIONS = Path("/data/options.json")
# RTSP socket timeout in microseconds: ffmpeg exits instead of hanging on a dead camera.
RTSP_TIMEOUT_US = "10000000"
HOUSEKEEPING_EVERY = 600
# How often a recorder checks whether the network storage is back.
MOUNT_RETRY = 30
VERSION = os.environ.get("NANO_NVR_VERSION", "dev")
STARTED = time.time()

# Last error per camera, shown in the panel: name -> (timestamp, message).
errors: dict = {}


@dataclass
class Camera:
    name: str
    url: str
    mode: str = "snapshot"
    snapshots_per_minute: float = 1.0
    fps: int = 0
    audio: bool = False
    live_url: str = ""

    @property
    def interval(self) -> float:
        """Seconds between two snapshots."""
        return 60 / self.snapshots_per_minute


@dataclass
class Settings:
    storage_path: Path
    cameras: list
    retention_days: int = 30
    min_free_gb: float = 5.0
    require_mount: bool = False
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
            snapshots_per_minute=float(c.get("snapshots_per_minute", 1)),
            fps=int(c.get("fps", 0)),
            audio=bool(c.get("audio", False)),
            live_url=with_credentials(c["live_url"], c.get("username", ""), c.get("password", ""))
            if c.get("live_url") else "",
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


def mount_point(path: Path) -> Path:
    """Network storage added in Home Assistant appears as /media/<name> or /share/<name>."""
    return Path(*path.parts[:3])


def storage_missing(s: Settings) -> bool:
    """With `require_mount`, nothing is written while the network storage is
    not mounted: the files would end up on Home Assistant's own disk."""
    return s.require_mount and not os.path.ismount(mount_point(s.storage_path))


def snapshots_paused(s: Settings) -> bool:
    """Below half of `min_free_gb` new snapshots are skipped. Housekeeping can
    only free past days, so this keeps room for building yesterday's timelapse
    instead of filling the disk with today's frames."""
    return bool(s.min_free_gb) and free_gb(s.storage_path) < s.min_free_gb / 2


def snapshot_loop(s: Settings, cam: Camera, stop: threading.Event) -> None:
    paused = False
    while not stop.is_set():
        started = time.monotonic()
        now = datetime.now()
        try:
            if storage_missing(s):
                if not paused:
                    log.error("%s: snapshots paused, %s is not mounted", cam.name, mount_point(s.storage_path))
                    errors[cam.name] = (time.time(), f"snapshots paused, {mount_point(s.storage_path)} is not mounted")
                paused = True
            elif snapshots_paused(s):
                if not paused:
                    log.error("%s: snapshots paused, free space under %s GB", cam.name, s.min_free_gb / 2)
                    errors[cam.name] = (time.time(), f"snapshots paused, free space under {s.min_free_gb / 2} GB")
                paused = True
            else:
                if paused:
                    log.info("%s: snapshots resumed", cam.name)
                paused = False
                grab_snapshot(s, cam, s.storage_path / cam.name / now.date().isoformat() / f"{now:%H%M%S}.jpg")
        except Exception as e:
            # A failing shot must never end the thread: the camera would stop until a restart.
            log.exception("%s: snapshot failed", cam.name)
            errors[cam.name] = (time.time(), f"snapshot failed: {e}")
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


def concat_videos(s: Settings, parts: list, video: Path) -> bool:
    """Join MP4s encoded with the same settings into `video`, without re-encoding."""
    tmp = video.with_name("." + video.name)
    with tempfile.NamedTemporaryFile("w", suffix=".txt") as playlist:
        for p in parts:
            playlist.write("file '%s'\n" % str(p).replace("'", r"'\''"))
        playlist.flush()
        cmd = [
            s.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-f", "concat", "-safe", "0", "-i", playlist.name,
            "-c", "copy", "-movflags", "+faststart", "-f", "mp4", str(tmp),
        ]
        res = subprocess.run(cmd, capture_output=True, preexec_fn=lower_priority)
    if res.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        log.error("%s: joining %s failed: %s", video.parent.name, video.name, last_line(res.stderr))
        return False
    tmp.rename(video)
    return True


def frames_to_timelapse(s: Settings, day_dir: Path) -> None:
    """Turn the frames of a day into its timelapse and delete them. When the
    timelapse already exists (today's frames were compacted to free space)
    the new frames are appended to it.

    Frames are first moved to a hidden folder: snapshots taken meanwhile land
    in the day folder and are neither encoded nor deleted by this round."""
    staging = day_dir / ".timelapse"
    pending = list(day_dir.glob("[0-9]*.jpg"))
    if not pending and not staging.is_dir():
        return
    staging.mkdir(exist_ok=True)
    for f in pending:
        f.rename(staging / f.name)
    frames = len(list(staging.glob("[0-9]*.jpg")))
    video = day_dir.with_suffix(".mp4")
    if not frames:
        ok = True
    elif not video.exists():
        ok = build_timelapse(s, staging, frames, video)
    else:
        part = video.with_name(".part-" + video.name)
        ok = build_timelapse(s, staging, frames, part) and concat_videos(s, [video, part], video)
        part.unlink(missing_ok=True)
    if ok:
        shutil.rmtree(staging, ignore_errors=True)
        return
    for f in staging.iterdir():  # keep them for the next round
        f.rename(day_dir / f.name)
    staging.rmdir()


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
    paused = False
    while not stop.is_set():
        if storage_missing(s):
            if not paused:
                log.error("%s: recording paused, %s is not mounted", cam.name, mount_point(s.storage_path))
                errors[cam.name] = (time.time(), f"recording paused, {mount_point(s.storage_path)} is not mounted")
            paused = True
            stop.wait(MOUNT_RETRY)
            continue
        paused = False
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
    if storage_missing(s):
        return
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
            if not s.keep_snapshots:
                frames_to_timelapse(s, entry)
            else:
                frames = list(entry.glob("[0-9]*.jpg"))
                video = entry.with_suffix(".mp4")
                if frames and not video.exists():
                    build_timelapse(s, entry, len(frames), video)
            try:
                entry.rmdir()  # only succeeds when nothing is left in the folder
            except OSError:
                pass
    enforce_free_space(s, today)


def enforce_free_space(s: Settings, today: date) -> None:
    if not s.min_free_gb or not s.storage_path.is_dir():
        return
    if free_gb(s.storage_path) < s.min_free_gb and not s.keep_snapshots:
        # Cheapest first: today's frames take far more room than their timelapse.
        for cam in s.cameras:
            day_dir = s.storage_path / cam.name / today.isoformat()
            if cam.mode == "snapshot" and any(day_dir.glob("[0-9]*.jpg")):
                log.warning("%s: free space under %s GB, compacting today's frames", cam.name, s.min_free_gb)
                frames_to_timelapse(s, day_dir)
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
# Live view: JPEG frames re-encoded by ffmpeg only while someone watches, capped
# in frame rate and height. The panel fetches one frame per request instead of
# an MJPEG stream because the ingress proxy drops the multipart boundary.
LIVE_FPS = 10
LIVE_HEIGHT = 720
# ffmpeg stops when no viewer has asked for a frame for this long.
LIVE_IDLE_SECONDS = 15
# How long a frame request waits for a frame newer than the one the viewer has.
LIVE_WAIT_SECONDS = 10
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
    missing = storage_missing(s) or not s.storage_path.is_dir()
    total, used, free = (0, 0, 0) if missing else shutil.disk_usage(s.storage_path)
    cameras = []
    for cam in s.cameras:
        err = errors.get(cam.name)
        cameras.append({
            "name": cam.name,
            "mode": cam.mode,
            "interval": cam.interval,
            "snapshots_per_minute": cam.snapshots_per_minute,
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
        "storage_missing": storage_missing(s),
        "segment_minutes": s.segment_minutes,
        "timelapse": {"fps": s.timelapse_fps, "quality": s.timelapse_quality,
                      "height": s.timelapse_height, "keep_snapshots": s.keep_snapshots},
        "disk": {"total": total, "used": used, "free": free},
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


def live_cmd(s: Settings, cam: Camera) -> list:
    return [
        s.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
        "-fflags", "nobuffer", "-flags", "low_delay",
        "-rtsp_transport", "tcp", "-timeout", RTSP_TIMEOUT_US, "-i", cam.live_url or cam.url,
        "-map", "0:v:0", "-an",
        "-vf", f"fps={LIVE_FPS},scale=-2:'min({LIVE_HEIGHT},ih)'",
        "-c:v", "mjpeg", "-q:v", "6", "-f", "image2pipe", "pipe:1",
    ]


def jpeg_frames(stream):
    """Split a stream of concatenated JPEGs. 0xFFD9 (end of image) cannot occur
    inside the entropy-coded data, where 0xFF bytes are always escaped."""
    buf = b""
    while chunk := stream.read1(65536):
        buf += chunk
        while (end := buf.find(b"\xff\xd9")) != -1:
            start = buf.find(b"\xff\xd8")
            if 0 <= start < end:
                yield buf[start:end + 2]
            buf = buf[end + 2:]


class LiveSession:
    """One ffmpeg per camera, shared by every viewer, alive while frames are asked for."""

    def __init__(self, s: Settings, cam: Camera):
        self.s, self.cam = s, cam
        self.cond = threading.Condition()
        self.frame, self.seq, self.done = None, 0, False
        self.last_request = time.monotonic()
        threading.Thread(target=self.run, name=f"live-{cam.name}", daemon=True).start()

    def run(self) -> None:
        # stderr to a file: a pipe nobody reads would stall ffmpeg once full.
        stderr = tempfile.TemporaryFile()
        proc = subprocess.Popen(live_cmd(self.s, self.cam), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=stderr, preexec_fn=lower_priority)
        log.debug("%s: live view started", self.cam.name)
        try:
            for frame in jpeg_frames(proc.stdout):
                with self.cond:
                    self.frame, self.seq = frame, self.seq + 1
                    self.cond.notify_all()
                if time.monotonic() - self.last_request > LIVE_IDLE_SECONDS:
                    break
        finally:
            proc.kill()
            proc.wait()
            proc.stdout.close()
            if not self.seq:
                stderr.seek(0)
                log.warning("%s: live view failed: %s", self.cam.name, last_line(stderr.read()))
            stderr.close()
            with self.cond:
                self.done = True
                self.cond.notify_all()
            log.debug("%s: live view stopped", self.cam.name)

    def next_frame(self, after: int):
        """Wait for a frame newer than `after`; returns (seq, jpeg) or None."""
        self.last_request = time.monotonic()
        with self.cond:
            if after > self.seq:
                after = 0  # the viewer's counter comes from a session that has ended
            self.cond.wait_for(lambda: self.seq > after or self.done, LIVE_WAIT_SECONDS)
            return (self.seq, self.frame) if self.frame else None


live_sessions: dict = {}
live_lock = threading.Lock()


def live_frame(s: Settings, cam: Camera, after: int):
    with live_lock:
        session = live_sessions.get(cam.name)
        if session is None or session.done:
            session = live_sessions[cam.name] = LiveSession(s, cam)
    return session.next_frame(after)


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
        elif path.startswith("/live/") and path.endswith(".jpg") and path[6:-4] in cams:
            after = (parse_qs(urlsplit(self.path).query).get("after") or ["0"])[0]
            frame = live_frame(self.settings, cams[path[6:-4]], int(after) if after.isdigit() else 0)
            if frame is None:
                return self.send_error(502)
            seq, jpeg = frame
            self.reply(jpeg, "image/jpeg", {"X-Frame-Seq": str(seq)})
        else:
            self.send_error(404)

    def reply(self, body: bytes, content_type: str, headers: dict = None) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

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
    if storage_missing(s):
        log.error("%s is not mounted: nothing is saved until it is", mount_point(s.storage_path))
    else:
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
            detail = f"{cam.snapshots_per_minute:g} per minute (every {cam.interval:g}s)"
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
