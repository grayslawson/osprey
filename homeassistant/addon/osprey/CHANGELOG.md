# Changelog

The changes to the Osprey Home Assistant app, newest version on top. The app
ships the Osprey controller, so the controller's own changelog is at
<https://github.com/grayslawson/osprey/blob/main/CHANGELOG.md>.

## 0.9.0b5

- Bundles controller 0.9.0b5. The optional Assist helper is not part of the app yet.

## 0.9.0b4

- Bundles controller 0.9.0b4.
- New options `onvif_username`, `onvif_password`, `rtsp_username`,
  `rtsp_password` and `security` (`auto`, `secure` or `legacy`). A new install
  generates the passwords, logs them once and requires them. An existing
  install keeps its behaviour until you set them.
- The base image is pinned by digest.

## 0.9.0b3

- The app can be installed: the store no longer rejects its manifest
  ("Docker image must not contain a tag").
- One init process instead of two, and no access to the Home Assistant
  configuration folder, which the app never used.

## 0.9.0b2

- The app starts under the Supervisor. It prepares its state directory on
  `/data` as root, then runs the controller as an unprivileged user.
- The sidebar console signs you in through Home Assistant.
- New `admin_password` option for the console on the direct port.
- The app no longer writes every camera message to an unredacted file.
- Bundles controller 0.9.0b2: a working setup wizard and MQTT commands, and a
  media-only ONVIF device for UniFi Protect.

## 0.9.0b1

First beta.

- The app installs a published image pinned to the add-on version, so an
  installation never runs an image from a different release.
- The Configuration tab shows readable option names instead of the raw keys.
- The app store shows the documentation, the changelog and the introduction.
- The app is staged as `experimental`: it works, but expect rough edges.
