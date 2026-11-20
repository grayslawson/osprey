# Home Assistant app repository

Home Assistant reads an app repository from the **root** of a Git repository:
a `repository.yaml` file at the root and one directory per add-on, named
after the add-on slug. The in-tree `homeassistant/addon/` layout is only a
source copy; adding the `grayslawson/osprey` URL in Home Assistant does not
list the app, because `repository.json` is not at the root of that repository.

The app is published from its own repository:
**[`grayslawson/osprey-ha-apps`](https://github.com/grayslawson/osprey-ha-apps)**.
Add that URL under **Settings > Apps > App store > ⋮ > Repositories**.

## What the mirror contains

| Path in the app repository | Source in the Osprey repository |
| --- | --- |
| `repository.yaml` | `packaging/homeassistant-repo/repository.yaml` |
| `osprey/config.yaml` | `homeassistant/addon/osprey/config.yaml`, with the version and the image tag written from `VERSION` |
| `osprey/run.sh` | `homeassistant/addon/osprey/run.sh` |
| `osprey/README.md` | `homeassistant/addon/osprey/README.md` |
| `osprey/Dockerfile` | written by the sync script; the build context is the app repository, where the application source is vendored |
| `osprey/src/osprey` | `osprey/` |
| `osprey/src/pyunifiwire` | `pyunifiwire/` |

The application source is vendored so the app repository can be built on its
own. The add-on installs a published image pinned to the add-on version, so
the image is built by the release workflow in this repository, not here.

## How the mirror is kept in step

`scripts/sync-ha-apps.sh` in this repository writes the mirror:

```sh
scripts/sync-ha-apps.sh <checkout-of-this-repository> <checkout-of-the-app-repository>
```

The script is idempotent, so it can run on a schedule and commit only when
something changed. The app repository runs it from
`.github/workflows/sync.yml`, on a daily schedule and on demand. A release
does not have to publish there: the next scheduled run picks the new version
up.

The `version:` and the image tag in the add-on manifest are written from
`VERSION` in this repository, and `homeassistant/validate.sh` checks that
both name the same version. An unpinned image tag would silently install
whichever image was published last.

Ingress is declared, but the web UI must honour `X-Ingress-Path` before the
add-on is listed as ingress-ready (tracked with the web-ui branch). Until then,
open the add-on through its published port.
