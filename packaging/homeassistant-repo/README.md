# Home Assistant app repository (scaffold)

Home Assistant reads an app repository from the **root** of a Git repository.
The in-tree `homeassistant/addon/` layout is only a source copy; adding the
`grayslawson/osprey` URL in Home Assistant does not list it, because
`repository.json` is not at the root of this repository.

To publish the app as its own repository (for example `osprey-ha-apps`):

1. Create the repository with `repository.yaml` at its root. This directory
   contains that file.
2. Copy `homeassistant/addon/osprey/` to `osprey/` in the new repository.
   Keep `config.yaml`, `Dockerfile`, `run.sh`, and `README.md` together.
3. Set `image:` in `osprey/config.yaml` to `ghcr.io/grayslawson/{arch}-osprey-addon`
   and keep `version:` equal to the Osprey release tag without the `v`.
4. Publish a release or tag in the apps repository. The release job in
   `.github/workflows/release.yml` (in the Osprey repository) is expected to
   open a pull request here that bumps `version:`. That automation is not
   implemented yet.

Ingress is declared, but the web UI must honour `X-Ingress-Path` before the
add-on is listed as ingress-ready (tracked with the web-ui branch). Until then,
open the add-on through its published port.
