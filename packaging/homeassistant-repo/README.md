# Home Assistant app repository

Home Assistant reads an app repository from the root of a Git repository: a
`repository.yaml` at the root and one directory per app, named after its
slug. This repository keeps the app in `homeassistant/addon/`, so adding the
`grayslawson/osprey` URL to Home Assistant lists nothing.

The app is published from
[`grayslawson/osprey-ha-apps`](https://github.com/grayslawson/osprey-ha-apps).
Users add that URL under **Settings → Apps → Install app → ⋮ →
Repositories**.

## What the mirror contains

| Path in the app repository | Source here |
| --- | --- |
| `repository.yaml` | `packaging/homeassistant-repo/repository.yaml` |
| `osprey/config.yaml` | `homeassistant/addon/osprey/config.yaml`, with the version and image tag written from `VERSION` |
| `osprey/run.sh` | `homeassistant/addon/osprey/run.sh` |
| `osprey/README.md`, `DOCS.md`, `CHANGELOG.md`, `translations/` | The same files in `homeassistant/addon/osprey/` |
| `osprey/Dockerfile` | Written by the sync script for the app repository's own build context |
| `osprey/src/osprey` | `osprey/` |
| `osprey/src/pyunifiwire` | `pyunifiwire/` |

The source is vendored so the app repository can build on its own. Installs
still pull the published image pinned to the app version, which the release
workflow in this repository builds.

## Keeping it in step

`scripts/sync-ha-apps.sh` writes the mirror:

```sh
scripts/sync-ha-apps.sh <checkout-of-this-repository> <checkout-of-the-app-repository>
```

It is idempotent, so the app repository runs it from
`.github/workflows/sync.yml` daily and on demand, and commits only when
something changed. The next scheduled run picks up a new release.

`homeassistant/validate.sh` checks that the manifest's `version:` and image
tag both match `VERSION`. An unpinned tag would install whichever image was
published last.

The repository URL is part of the app's identity: the Supervisor derives the
app's ID from it. Keep `grayslawson/osprey-ha-apps` where it is.
