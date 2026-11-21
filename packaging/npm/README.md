# `@osprey-ptz/cli`

Runs Osprey's read-only tools from the published container image. It does
not install Docker or Podman and never starts the controller.

```sh
npm install --global @osprey-ptz/cli@next   # pre-releases; drop @next for stable
export OSPREY_RUNTIME=docker                # or podman
export OSPREY_IMAGE=ghcr.io/grayslawson/osprey:0.9.0b2
osprey doctor --host 192.0.2.10 --camera 192.0.2.20 \
  --frigate http://frigate.example.com:5000
```

| Command | What it does |
|---|---|
| `doctor` | Checks the host, ports, camera route and Frigate. |
| `probe` | Writes a redacted data bundle for an unsupported camera model. |
| `protect-guard` | Checks for, or with `--apply` creates, the UniFi firewall rule that keeps Protect off the camera. |
| `version` | Prints the Osprey version in the image. |

Each command runs `docker run --rm --network host` with `OSPREY_IMAGE`. Pin it
to a release tag or digest. To deploy the controller itself, use
`compose.release.yaml`; see the
[installation guide](https://github.com/grayslawson/osprey/blob/main/docs/installation.md).
