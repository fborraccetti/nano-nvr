# Changelog

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
