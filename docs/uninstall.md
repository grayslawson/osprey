# Uninstall and return to UniFi Protect

This page takes the camera back to Protect and removes Osprey. Do the camera
first, while Osprey is still running, so you can check each step.

Osprey never flashes firmware or logs in to the camera over SSH. Adoption sets
the camera's controller and stream settings, and a factory reset clears them.

## 1. Remove what Osprey added around Protect

Skip any step you never set up.

1. **Media persona.** If you adopted `<camera name> (media)` in Protect as a
   third party camera, remove it there first. See
   [Protect alongside Frigate](protect-dual-adoption.md).
2. **Guard policy.** Protect cannot adopt the camera while the firewall rule
   blocks it. In UniFi Network, delete the policy named
   `Osprey guard: camera to Protect` (under **Settings → Policy Engine →
   Zones**, or **Security → Firewall** on older releases). If you made the
   rule by hand, delete that instead. [Protect guard](protect-guard.md) describes the rule.
3. **UniFi API key.** If you made one for `osprey protect-guard`, delete it
   in **Settings → Control Plane → Integrations**, and delete the key file.

## 2. Give the camera back

1. **Stop Osprey**, or at least stop it from serving this camera. The
   [section below](#3-remove-osprey) has the command for each install path.
   While Osprey runs, the camera keeps reconnecting to it.
2. **Factory-reset the camera** with its reset button, following Ubiquiti's
   instructions for your model. It remembers Osprey as its controller until
   then. After the reset it is back on the default `ubnt`/`ubnt` login and has
   no controller.
3. **Adopt it in Protect.** It shows up under **UniFi Devices** with an
   **Adopt** button, as long as Protect can reach it. Adopt it as usual.
4. **Check** live view and a recording in Protect.

Protect then manages the camera natively again, with its own PTZ controls.
Frigate can no longer autotrack it through Osprey. If you want Frigate to keep
the camera, remove the Osprey camera entry from the Frigate configuration, or
point it at a source that still exists.

## 3. Remove Osprey

### Container

Stop the service and keep the state volume, in case you come back:

```sh
docker compose -f compose.release.yaml down
```

To delete the state too, add `-v`. This removes the certificate the camera
trusted, your `osprey.json`, the generated passwords, presets, patrols and
the Frigate URL:

```sh
docker compose -f compose.release.yaml down -v
```

Then delete `compose.release.yaml`, your `.env` file, any
`compose.override.yaml` and `osprey.json` copy you made, and the image with
`docker image rm ghcr.io/grayslawson/osprey:TAG`. If you installed the update
timers from `contrib/systemd`, disable them:

```sh
sudo systemctl disable --now osprey-update-check.timer
```

(Use `osprey-update-apply.timer` too, if you installed it.) Delete the unit
files you copied into `/etc/systemd/system/` and the helper in
`/usr/local/libexec/`.

### Home Assistant app

Stop the app, then open it in **Settings → Apps** and select **Uninstall**.
Its data (the certificate, presets and patrols) lives in `/data/osprey` inside
the app, and Home Assistant removes it when you uninstall the app. Remove the
repository from the app store's **Repositories** list if you want it gone too.
Anything you added in Frigate for the camera stays; remove it there.

### NixOS

Set `services.osprey.enable = false;`, remove the module and the flake input,
and rebuild. The state directory `/var/lib/osprey` stays on disk. Delete it
yourself once you are sure you are done:

```sh
sudo rm -r /var/lib/osprey
```

### Python wheel

```sh
pip uninstall osprey-ptz
```

Delete the files you passed with `--config` and `--cert`, and the files the
setup wizard wrote beside the config (`osprey-secrets.env`, `rtsp-password`,
`onvif-password`, `frigate.json`), plus `osprey-presets.json` and
`sentry.json` in the certificate's directory.

### Homebrew, Chocolatey and npm

These only install diagnostics tools or the release Compose file.

```sh
brew uninstall osprey
```

```powershell
choco uninstall osprey
```

```sh
npm uninstall --global @osprey-ptz/cli
```

Chocolatey leaves `%ProgramData%\osprey` if you created a `.env` file there.
Delete it by hand.

### Proxmox

If you used `scripts/proxmox-install.sh`, it wrote `compose.yaml` under its
state directory (`/var/lib/osprey` unless you changed it). Run `down` on that
file, as for the container above, and remove the directory.

## 4. Clean up around it

- Delete the firewall rules you opened for TCP 7442, 7444, 7550, 8000 and
  8554, and UDP 3702.
- Remove the DHCP reservation for the Osprey host, if it was only for this.
- Remove Osprey's MQTT user and its ACLs from your broker, if you enabled
  MQTT.
- Revoke the console password and ONVIF or RTSP passwords anywhere you stored
  them.
