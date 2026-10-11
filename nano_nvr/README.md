# Nano NVR

Lightweight RTSP recorder for Home Assistant. It saves your cameras to
`/media` as daily timelapses or continuous recordings, so you can drop the
vendor cloud and the SD cards.

* **snapshot mode**: one frame every N seconds, turned into one timelapse MP4
  per day. Almost no CPU.
* **record mode**: the stream copied to MP4 segments without re-encoding.
* Retention in days and a minimum free space guard.
* Browse everything from the Home Assistant **Media** panel.
* **NVR** sidebar panel: camera mosaic, status and storage at a glance.

See the **Documentation** tab for configuration and camera setup.
