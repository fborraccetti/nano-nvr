# Nano NVR

Nano NVR saves the footage of your IP cameras to Home Assistant storage, so
you need neither the vendor cloud nor an SD card in each camera. It talks to
the cameras over RTSP on your local network and does as little work as
possible: no motion detection, no live streaming, no database. You browse the
files from the **Media** panel of Home Assistant, and an **NVR** entry in the
sidebar shows how the add-on is doing.

## How it works

Each camera runs in one of two modes.

**snapshot** (default): every `interval` seconds the add-on opens the stream,
takes one keyframe and closes it again. Nothing is decoded between two shots,
so the load is close to zero. Shortly after midnight the frames of the day
become a timelapse MP4 (H.264) and the JPGs are deleted.

**record**: the stream is written to disk as MP4 segments of
`segment_minutes`. The video is copied as it arrives, without re-encoding, so
the load stays low (a few percent of a CPU core per camera), but storage grows
with the camera bitrate.

Files are organised like this:

```text
/media/nano_nvr/
  garden/2026-10-11/143000.jpg    snapshots of today (snapshot mode)
  garden/2026-10-10.mp4           timelapse of a finished day
  gate/2026-10-11/143000.mp4      recorded segment (record mode)
```

Every 10 minutes the add-on deletes days older than `retention_days` and, if
free space is below `min_free_gb`, the oldest days of any camera until there
is enough space again. Today's files are never deleted.

## Installation

1. In Home Assistant go to **Settings → Add-ons → Add-on store**, open the
   menu at the top right, choose **Repositories** and add the URL of this
   repository.
2. Install **Nano NVR**, add your cameras in the **Configuration** tab and
   start it.
3. Check the **Log** tab: every camera prints one line at startup.
4. Open **Media → My media → nano_nvr** to browse the files.

## Status panel

The **NVR** entry in the sidebar opens a single page with:

* a summary: cameras that are saving, space used by the footage, free disk
  space, retention and the estimated size once retention is full;
* a mosaic with a preview of every camera, the time of the last saved file and
  the last error, if any. Click a camera (or **Live view**) to watch it live in
  a popup; **Full image** opens the preview at full size in a new tab;
* storage per camera: days kept, oldest and newest day, timelapses, space
  used, average per day and estimate at full retention;
* the current settings (credentials are hidden).

The page refreshes every 30 seconds. Previews come from the latest snapshot;
for cameras in record mode, or when no recent snapshot exists, a fresh frame is
taken, at most once a minute and only while the page is open. The panel is
served through Home Assistant ingress only, no port is opened on your network.

The live view is an MJPEG stream (up to 10 fps, 720p) encoded by ffmpeg only
while the popup is open: closing it stops ffmpeg right away, and a stream left
open stops by itself after 30 minutes. It costs CPU while you watch, so prefer
the snapshot preview on very small boards.

## Configuration

```yaml
storage_path: /media/nano_nvr
retention_days: 30
min_free_gb: 5
segment_minutes: 10
timelapse_fps: 24
timelapse_quality: 28
timelapse_height: 0
keep_snapshots: false
log_level: info
cameras:
  - name: garden
    url: rtsp://192.168.1.10:554/ch1/main
    username: admin
    password: ABCDEF
    mode: snapshot
    interval: 60
  - name: gate
    url: rtsp://192.168.1.11:554/ch1/main
    username: admin
    password: GHIJKL
    mode: record
```

### General options

| Option | Default | Description |
| --- | --- | --- |
| `storage_path` | `/media/nano_nvr` | Where files are written. Must be under `/media` or `/share`. |
| `retention_days` | `30` | Days to keep, today included. `0` keeps everything. |
| `min_free_gb` | `5` | Delete the oldest days when free space drops below this. `0` disables it. |
| `segment_minutes` | `10` | Length of each file in record mode. Cuts are aligned to the clock. |
| `timelapse_fps` | `24` | Frame rate of the daily timelapse. |
| `timelapse_quality` | `28` | H.264 CRF of the timelapse: lower means better and bigger. |
| `timelapse_height` | `0` | Resize the timelapse to this height (e.g. `720`). `0` keeps the original. |
| `keep_snapshots` | `false` | Keep the JPGs after the timelapse is built. |
| `log_level` | `info` | `debug`, `info`, `warning` or `error`. |

### Camera options

| Option | Default | Description |
| --- | --- | --- |
| `name` | required | Folder name: letters, digits, `-` and `_`. |
| `url` | required | RTSP URL, without credentials. |
| `username`, `password` | empty | Added to the URL; special characters such as `@` are handled. |
| `mode` | `snapshot` | `snapshot` or `record`. |
| `interval` | `60` | Seconds between two snapshots. Lower gives smoother timelapses. |
| `fps` | `0` | Record mode only. `0` copies the stream as is; any other value re-encodes to H.264 at that frame rate. |
| `audio` | `false` | Record mode only. Keep the audio track (converted to AAC). |
| `enabled` | `true` | Set to `false` to pause a camera without deleting it. |

### Choosing the level of detail

From lightest to heaviest:

1. **snapshot, `interval: 60`**: one frame per minute, a one-minute video per
   day. Good for "what happened today" at a glance.
2. **snapshot, `interval: 5` to `10`**: smoother timelapse, still almost no
   CPU. Below 5 seconds the time needed to open the stream becomes the limit.
3. **record**: every frame, as the camera sends it. Use the camera's
   sub-stream (lower resolution) if storage is a concern.
4. **record with `fps`**: fewer frames per second, smaller files, but every
   frame is decoded and encoded again. This is the only option that costs real
   CPU: use it only if your hardware has room for it.

## Storage and load

Rough figures for one 1080p camera:

| Setup | Disk per day | CPU |
| --- | --- | --- |
| snapshot, 60 s | ~600 MB during the day, then 20-50 MB of timelapse | ~0, plus 1-2 min of encoding after midnight |
| record, 2 Mbit/s stream | ~20 GB | a few % of one core |
| record, `fps: 5` | depends on the scene, often 3-8 GB | 20-50 % of one core |

Timelapses are built one camera at a time at the lowest CPU priority.

## Camera setup

### Finding the RTSP URL

Most cameras accept one of these paths (port 554):

| Brand | Main stream | Sub-stream |
| --- | --- | --- |
| EZVIZ, Hikvision | `/Streaming/Channels/101` or `/ch1/main` | `/Streaming/Channels/102` or `/ch1/sub` |
| Dahua, Amcrest | `/cam/realmonitor?channel=1&subtype=0` | `/cam/realmonitor?channel=1&subtype=1` |
| Reolink | `/h264Preview_01_main` | `/h264Preview_01_sub` |
| TP-Link Tapo | `/stream1` | `/stream2` |

You can test a URL with VLC (**Media → Open Network Stream**) before adding it.

### EZVIZ

* RTSP must be enabled in the EZVIZ app (camera settings → **LAN Live View**
  or **RTSP**, depending on the firmware).
* The user is `admin` and the password is the 6-letter **verification code**
  printed on the camera label.
* Some models only answer on `rtsp://IP:554` with no path.

### Codecs and playback

Recordings keep the codec of the camera. H.264 plays everywhere. H.265 (HEVC)
saves space but plays only in browsers with hardware HEVC support (Safari,
recent Chrome and Edge, not Firefox). If you want to watch recordings from any
browser, set the camera to H.264 in its app. Timelapses are always H.264.

### Going cloud-free

Nano NVR only needs the local RTSP stream. Once it is running you can:

* remove the SD card from the cameras;
* block internet access for the cameras on your router (a firewall rule or a
  separate VLAN). The vendor app will stop working outside your home: use Home
  Assistant for live view and remote access instead.

Keep internet access open for a while after the first setup if you still need
the vendor app to change camera settings or update the firmware.

## Backups

Home Assistant backups include `/media` by default. With weeks of footage they
become very large: in **Settings → System → Backups → Backup settings**
exclude the Media folder, or set `storage_path` to a network share mounted
under `/media` or `/share`.

## Troubleshooting

* **`snapshot failed: ... 401 Unauthorized`**: wrong username or password.
* **`snapshot timed out`** or **`Connection refused`**: wrong IP or port, RTSP
  disabled on the camera, or the camera is offline.
* **`recorder exited`**: the camera dropped the connection. The add-on retries
  after 5 seconds, then waits longer and longer (up to 5 minutes) while the
  camera stays unreachable.
* **The last minutes before a restart are missing or cut**: when the add-on
  stops, the segment being written loses at most a few seconds. The files are
  fragmented MP4, so everything before that point plays normally.
