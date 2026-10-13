# Changelog

## 0.5.3

* Fix: a full disk stopped snapshot cameras for good. An error while taking a
  snapshot (such as `No space left on device` when creating the day folder at
  midnight) ended the camera thread until the add-on was restarted; now it is
  logged and the next snapshot is tried as usual.
* Snapshot cameras pause when free space drops under half of `min_free_gb`
  and resume by themselves. Before, today's frames could fill the disk, the
  timelapse of the day before failed for lack of space and its frames were
  then deleted to free space.

## 0.5.2

* Panel: links to the storage folder and to each camera's folder in the Home
  Assistant Media browser (summary, camera cards, retention table, settings).
  Shown only when the storage path is under /media.

## 0.5.1

* `interval` removed: the snapshot rate of each camera is set only with
  `snapshots_per_minute`. A camera that still has `interval` must be changed
  by hand, for example `interval: 60` becomes `snapshots_per_minute: 1` and
  `interval: 5` becomes `snapshots_per_minute: 12`.

## 0.5.0

* New camera option `snapshots_per_minute` (default 1, decimals allowed) makes
  the snapshot rate explicit. The old `interval` in seconds still works when
  `snapshots_per_minute` is not set.
* Configuration tab reorganised: cameras first, then storage, snapshot mode
  and timelapse, record mode. The timelapse frame rate now says it only sets
  playback speed.

## 0.4.1

* Fix live view through Home Assistant ingress ("Live view unavailable"): the
  ingress proxy drops the boundary of MJPEG streams, so the panel now fetches
  one frame at a time. One ffmpeg per camera is shared by all viewers and stops
  about 15 seconds after the last one leaves.
* New camera option `live_url` to use a lighter sub-stream for the live view.

## 0.4.0

* Live view: click a camera in the panel to watch it live in a popup (MJPEG,
  up to 10 fps and 720p, encoded only while the popup is open). Each camera
  card also links to the full size preview in a new tab.

## 0.3.0

* First public release as Nano NVR (slug `nano_nvr`, default storage path
  `/media/nano_nvr`).
* Add-on icon and logo.

## 0.2.0

* Status panel in the Home Assistant sidebar ("NVR"): summary, live preview
  mosaic of all cameras, storage and retention per camera, current settings.

## 0.1.1

* Fix startup on Home Assistant: use a fixed Alpine base image instead of the
  Supervisor's default s6-overlay base ("s6-overlay-suexec: fatal: can only run
  as pid 1").

## 0.1.0

* First release: snapshot mode with daily timelapses, record mode with MP4
  segments, retention in days, minimum free space guard.
