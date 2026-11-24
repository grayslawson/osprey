# `@osprey-ptz/cli`

Runs Osprey's read-only tools from the published container image. It does
not install Docker or Podman and never starts the controller.

npm publishing starts with a later release; pre-releases will then be on the
`next` dist-tag (`npm install --global @osprey-ptz/cli@next`). Until then,
install the tarball attached to the GitHub release:

```sh
npm install --global https://github.com/grayslawson/osprey/releases/download/v0.9.0b5/osprey-ptz-cli-0.9.0b5.tgz
export OSPREY_RUNTIME=docker                # or podman
export OSPREY_IMAGE=ghcr.io/grayslawson/osprey:0.9.0b5
osprey doctor --host 192.0.2.10
```

| Command | What it does |
|---|---|
| `doctor` | Checks the advertised host. |
| `probe` | Writes a redacted data bundle for an unsupported camera model. |
| `protect-guard` | Checks for, or with `--apply` creates, the UniFi firewall rule that keeps Protect off the camera. |
| `version` | Prints the Osprey version in the image. |

Each command runs `docker run --rm --network host` with `OSPREY_IMAGE` and no
host files mounted. Pin the image to a release tag or digest. `doctor` checks
the camera route, callback and Frigate from there, but it sees no config file
and the image has no `ss`, so the config and local port checks need the Python
wheel or a source checkout on the Osprey host. For the same reason `probe --dump` and
`protect-guard --api-key-file` cannot read files on the host. To deploy the
controller itself, use `compose.release.yaml`; see the
[installation guide](https://github.com/grayslawson/osprey/blob/main/docs/installation.md).

Osprey is an independent project, not affiliated with or endorsed by Ubiquiti
Inc. UniFi and UniFi Protect are trademarks of Ubiquiti Inc.
