# Upgrading

Osprey keeps everything it needs between versions in its state directory: the
certificate the camera trusts, `osprey.json`, the generated secrets, presets
and patrols. Upgrading means running a newer image or package against the same
state. The [changelog](../CHANGELOG.md) and each
[GitHub release](https://github.com/grayslawson/osprey/releases) list what
changed.

Every 0.9 release so far is a beta. Read the notes for each version you skip
over, and test with the camera you care about before you rely on a new beta.

## Before you upgrade

1. Read the release notes, looking for a **Breaking** section.
2. Copy the state out of the container so you can go back:

   ```sh
   docker compose -f compose.release.yaml cp osprey:/var/lib/osprey ./osprey-state-backup
   ```

   The copy holds secrets. Keep it `0600` or on an encrypted volume.
3. Keep the previous image tag in your notes. The update helper never rolls
   back on its own.

## Container

Pin the new tag, then let the helper check and apply it. Without `--apply` it
only validates the Compose file and prints the image it would use:

```sh
scripts/osprey-update.sh --file compose.release.yaml --env-file .env \
  --image ghcr.io/grayslawson/osprey:NEW_VERSION
scripts/osprey-update.sh --file compose.release.yaml --env-file .env \
  --image ghcr.io/grayslawson/osprey:NEW_VERSION --apply
```

Or edit `OSPREY_IMAGE` in your `.env` and run `docker compose -f
compose.release.yaml up -d`. Check `/healthz` and the Health tab afterwards,
then watch a Frigate autotracking test.

To roll back, run the same command with the previous tag. Older releases
ignore state files they do not know, such as `osprey-presets.json`, but
re-save presets and patrols if you changed them on the newer one.

## Home Assistant app

Update it from **Settings → Apps**. The options and the data in
`/data/osprey` stay as they are.

## NixOS

Change the version in the flake input (`github:grayslawson/osprey/vNEW_VERSION`)
and rebuild. The state directory `/var/lib/osprey` is reused.

## Python wheel

Install the newer wheel into the same virtual environment, then restart the
service:

```sh
pip install --upgrade "osprey-ptz[mqtt] @ https://github.com/grayslawson/osprey/releases/download/vNEW_VERSION/osprey_ptz-NEW_VERSION-py3-none-any.whl"
```

Replace `NEW_VERSION` with the release you want, as in `0.9.0b3`.

## Notes for older installs

These apply if you ran Osprey before 0.9 or from an early checkout.

- The controller moved from `cuckoo/` to `osprey/`. Anything that starts
  `cuckoo/main.py` has to start `osprey/main.py`. The image, the Home
  Assistant app, the NixOS package and the wheel already do.
- File names changed. The defaults are `osprey.json` and `osprey.pem`.
  Osprey still reads an existing `cuckoo.json` or `cuckoo.pem` when the new
  file is missing, so a camera keeps trusting its certificate. Keep that file.
- Logger names changed from `cuckoo.*` to `osprey.*`. Update any log
  filters or alerts.
- `handoff.sh` variables. The script reads `OSPREY_*` and falls back to
  the old `CUCKOO_*` names. The controller itself reads only `OSPREY_*`.
- Presets. The first start of a release with the preset table creates
  `osprey-presets.json`. Presets saved before then lived only in memory, so
  save them again. See [PTZ health](ptz-health.md#home-preset-and-persistence).
- **The discovery UUID did not change**, so NVRs keep seeing the same device
  after the rename.
