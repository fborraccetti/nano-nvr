#!/usr/bin/env python3
"""Serve the status panel on sample data, for screenshots.

    python3 tools/demo_panel.py FRAMES_DIR [--port 18099] [--days 12]

FRAMES_DIR holds one JPG per camera, named after the camera (front_gate.jpg,
garage.jpg, ...). The script builds a temporary storage folder with today's
snapshots and `--days` past timelapses of realistic size (sparse files, so
they take no real disk space), then serves only the panel: no camera is
contacted.
"""

import argparse
import os
import shutil
import sys
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "nano_nvr" / "app"))
os.environ.setdefault("NANO_NVR_VERSION", "0.3.0")

import nano_nvr as nvr  # noqa: E402

GB = 1000 ** 3


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("frames", type=Path)
    ap.add_argument("--port", type=int, default=18099)
    ap.add_argument("--days", type=int, default=12)
    args = ap.parse_args()

    root = Path(tempfile.mkdtemp(prefix="nano_nvr_demo_"))
    today = date.today()
    cameras = []
    for i, frame in enumerate(sorted(args.frames.glob("*.jpg"))):
        name = frame.stem
        cameras.append(nvr.Camera(name=name, url=f"rtsp://admin:x@192.168.1.{21 + i}:554/ch1/main"))
        day_dir = root / name / today.isoformat()
        day_dir.mkdir(parents=True)
        # Today's snapshots so far, one per minute, the newest being the real frame.
        minutes = 60 * 18
        for m in range(minutes):
            with (day_dir / f"{m // 60:02d}{m % 60:02d}00.jpg").open("wb") as f:
                f.truncate(450_000)
        shutil.copy(frame, day_dir / "180000.jpg")
        for d in range(1, args.days + 1):
            video = root / name / f"{(today - timedelta(days=d)).isoformat()}.mp4"
            with video.open("wb") as f:
                f.truncate(int((16 + 5 * i + 6 * ((3 * d + i) % 5)) * 1000 ** 2))

    s = nvr.Settings(storage_path=root, cameras=cameras, retention_days=30, min_free_gb=5)
    nvr.STARTED = time.time() - 3 * 86400 - 5 * 3600
    # A typical Home Assistant disk instead of the machine running the demo.
    nvr.shutil.disk_usage = lambda _: shutil._ntuple_diskusage(64 * GB, 27 * GB, 37 * GB)
    real_status = nvr.status
    nvr.status = lambda s: {**real_status(s), "storage_path": "/media/nano_nvr"}
    nvr.PanelHandler.settings = s
    server = nvr.ThreadingHTTPServer(("127.0.0.1", args.port), nvr.PanelHandler)
    print(f"Demo panel on http://127.0.0.1:{args.port}/ (data in {root})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
