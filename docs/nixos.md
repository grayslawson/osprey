# NixOS packaging

`flake.nix` exports `packages.<system>.default` and `nixosModules.default`. The
package is built from the source tree and its version comes from the root
`VERSION` file. The module runs Osprey as a DynamicUser systemd service:

- writable state (certificate, first-run wizard overlay, `frigate.json`) lives
  in `/var/lib/osprey`, which systemd creates and owns;
- the generated configuration is a read-only file in the Nix store;
- admin, ONVIF, and RTSP passwords are read from files with `LoadCredential`,
  so they never enter the store;
- `openssl` and `ffmpeg` are on the service PATH through the package wrapper.

Add the flake to a NixOS host configuration:

```nix
{
  inputs.osprey.url = "github:grayslawson/osprey";

  outputs = { self, nixpkgs, osprey, ... }: {
    nixosConfigurations.example = nixpkgs.lib.nixosSystem {
      system = "x86_64-linux";
      modules = [
        osprey.nixosModules.default
        ({ config, ... }: {
          services.osprey = {
            enable = true;
            host = "192.0.2.10";                 # address reachable by the camera and clients
            openFirewall = true;                 # optional; see below
            adminPasswordFile = config.sops.secrets.osprey-admin.path;
            rtspPasswordFile = config.sops.secrets.osprey-rtsp.path;
            ports.onvif = 8000;
            ports.rtsp = 8554;
          };
        })
      ];
    };
  };
}
```

## Options

| Option | Purpose |
|---|---|
| `enable` | Start the service. |
| `host` | Concrete LAN address the camera and clients use. Wildcards are rejected by an assertion. |
| `announce` | Send and answer WS-Discovery on UDP 3702 (default `true`). |
| `ports.*` | Listener ports; defaults are control 7442, ingest 7550, snapshot 7444, RTSP 8554, ONVIF 8000, discovery 3702. |
| `adminPasswordFile`, `onvifPasswordFile`, `rtspPasswordFile` | Paths to secret files, usually from sops-nix or agenix. Each is loaded through `LoadCredential`. |
| `openFirewall` | Open the TCP ports for control, ingest, snapshot, RTSP, and ONVIF, and UDP for discovery. Default `false`. |
| `package` | Override the Osprey package. |

The MQTT password is not read from a file by this module yet. Keep MQTT
disabled in NixOS deployments until that is added, or set it through the
first-run wizard.

## Networking

The camera dials `host` on TCP 7442, 7550, and 7444. Clients use TCP 8000 and
8554 and multicast UDP 3702. `services.osprey.host` must be the host's LAN
address, never a bridge or loopback address. With `openFirewall = false`, open
the same ports in your own firewall policy.

The module does not manage camera credentials, adoption, Frigate, or Protect.

## Checks

Evaluate and build without contacting a camera:

```sh
nix flake check               # builds the package
nixos-rebuild dry-build --flake .#example
```

The VM test that boots the module and checks `/healthz` is planned for Phase 2
(see `specs/002-python-package-cli`); it is not in the flake yet.

Keep the flake input pinned with a lock file and review Osprey releases before
updating it. Use your deployment's secret manager for admin, camera, and ONVIF
credentials; never put those values in the flake.
