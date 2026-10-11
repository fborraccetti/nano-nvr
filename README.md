# Nano NVR

A Home Assistant add-on that records IP cameras over RTSP to local storage,
with as little CPU, memory and moving parts as possible.

Most cameras push their footage to a vendor cloud or to an SD card inside the
camera. Nano NVR pulls the stream over your LAN instead and writes plain
files to `/media`, where Home Assistant's Media panel can play them.

* **snapshot mode**: one keyframe every N seconds, turned into one H.264
  timelapse per day. The stream is opened only for the shot, so the load is
  close to zero.
* **record mode**: continuous recording in MP4 segments, copied without
  re-encoding. Optionally re-encoded at a lower frame rate.
* Retention in days, plus a free space guard that removes the oldest days
  first.
* A sidebar panel with a preview mosaic, camera status and storage per camera.
* One Python file, standard library only, plus ffmpeg on Alpine.

It is not a full NVR: no motion detection, no object detection, no live
streaming.
If you need those, look at [Frigate](https://frigate.video) or
[motionEye](https://github.com/motioneye-project/motioneye).

## Installation

[![Add repository to Home Assistant][repo-badge]][repo-link]

Or manually: **Settings → Add-ons → Add-on store → ⋮ → Repositories**, add
`https://github.com/OWNER/nano-nvr`, then install **Nano NVR**.

Full documentation: [nano_nvr/DOCS.md](nano_nvr/DOCS.md).

## Development

```sh
# unit tests (standard library only)
python3 -m unittest discover -s tests -v

# run against real cameras without Home Assistant
python3 nano_nvr/app/nano_nvr.py --options my-options.json

# build the add-on image
docker build --build-arg BUILD_ARCH=amd64 -t nano-nvr nano_nvr
```

`my-options.json` uses the same keys as the add-on configuration, as JSON.
Set `storage_path` to a local folder. Environment variables for local runs:

* `FFMPEG`: path of a different ffmpeg binary;
* `NANO_NVR_PANEL_PORT`: panel port (default 8099);
* `NANO_NVR_PANEL_OPEN=1`: accept panel requests from any address, not only
  from the Home Assistant ingress proxy (handy inside Docker);
* `NANO_NVR_PREVIEW_DIR`: where live previews are cached (default
  `/tmp/nano_nvr_preview`).

## License

MIT, see [LICENSE](LICENSE).

[repo-badge]: https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg
[repo-link]: https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2FOWNER%2Fnano-nvr
