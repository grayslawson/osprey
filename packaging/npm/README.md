# `@osprey-ptz/cli`

This package installs the `osprey` command. It runs Osprey's diagnostics in a
published container; it does not install Docker/Podman or silently start a
camera controller.

```sh
npm install --global @osprey-ptz/cli
export OSPREY_RUNTIME=docker       # or podman
export OSPREY_IMAGE=ghcr.io/grayslawson/osprey:0.1.0
osprey doctor --host 192.0.2.10 --camera 192.0.2.20 \
  --frigate http://frigate.local:5000
```

Use `compose.release.yaml` from the Osprey repository for the deployment and
pin `OSPREY_IMAGE` to a release tag or digest. The Homebrew and Chocolatey
integrations follow the same container-only model.
