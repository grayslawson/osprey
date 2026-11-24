# Vendored code

`raptor_vision/` is the core of [raptor-vision](https://github.com/grayslawson/raptor-vision) (MIT), copied unchanged at the
tag in `raptor_vision/VERSION`: `types`, `providers`, `tasks`, `labels`, `label`, `geometry`, `interest` and `frigate`. It is
standard library plus Pillow. The optional extras (`segment`, `entry`) are not copied.

It is vendored, not installed, because Osprey runs from this source tree on every install path (Docker image, Home Assistant
app, Nix package, PyPI wheel, Proxmox script) and PyPI refuses a dependency on a git URL. Vendoring needs no network at runtime
or at install time.

Update with `scripts/sync-raptor-vision.sh vX.Y.Z`. Do not edit the copy by hand; fix it upstream and sync.
