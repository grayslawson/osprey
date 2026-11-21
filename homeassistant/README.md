# Home Assistant app

`addon/osprey` is the source of the Osprey Home Assistant app. Users install
it from the separate app repository,
[`grayslawson/osprey-ha-apps`](https://github.com/grayslawson/osprey-ha-apps);
see [`packaging/homeassistant-repo`](../packaging/homeassistant-repo/README.md)
for how that repository is kept in step.

| File | Shown in Home Assistant as |
|---|---|
| `addon/osprey/README.md` | The app store introduction |
| `addon/osprey/DOCS.md` | The Documentation tab |
| `addon/osprey/CHANGELOG.md` | The Changelog tab |
| `addon/osprey/translations/en.yaml` | Configuration tab labels |

## Build and check

The Dockerfile's build context is the repository root, because it copies
`osprey/` and `pyunifiwire/`:

```sh
docker build -f homeassistant/addon/osprey/Dockerfile -t osprey-addon:local .
sh homeassistant/validate.sh
```

`validate.sh` checks the manifest, the files the app store reads, and that the
manifest version and image tag both match `VERSION`. The release workflow
builds the published images, `ghcr.io/grayslawson/{arch}-osprey-addon:<version>`,
for `amd64` and `aarch64`.

Before a release, test a clean install and an upgrade, ingress, persistence in
`/data`, Frigate ingest, PTZ and tracking on each architecture.
