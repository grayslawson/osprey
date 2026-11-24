# NixOS

`flake.nix` exports `packages.<system>.default` and `nixosModules.default`.
The package builds from the source tree, with its version from `VERSION`. The
module runs Osprey as a hardened `DynamicUser` systemd service:

- State (certificate, presets, patrols, `frigate.json`) lives in
  `/var/lib/osprey`, which systemd creates and owns.
- The configuration is generated into the Nix store and is read-only.
- Admin, ONVIF and RTSP passwords are read from files with `LoadCredential`,
  so they never enter the store.
- The package wrapper puts `openssl` and `ffmpeg` on the service's `PATH`.

Add the flake to a NixOS host configuration:

```nix
{
  inputs.osprey.url = "github:grayslawson/osprey/v0.9.0b6";

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
          # A password file needs its user name, or Osprey refuses to start.
          systemd.services.osprey.environment.OSPREY_RTSP_USERNAME = "osprey";
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
| `stateDirectory` | Name of the state directory under `/var/lib`. Read-only, `osprey`. |
| `ports.*` | Listener ports; defaults are control 7442, ingest 7550, snapshot 7444, RTSP 8554, ONVIF 8000, discovery 3702. |
| `adminPasswordFile`, `onvifPasswordFile`, `rtspPasswordFile` | Paths to secret files, usually from sops-nix or agenix. Each is loaded through `LoadCredential`. The module has no user name options: set `OSPREY_RTSP_USERNAME` and `OSPREY_ONVIF_USERNAME` (use `frigate`, see [Frigate](frigate-integration.md#manual-control-and-autotracking)) in `systemd.services.osprey.environment`. |
| `openFirewall` | Open the TCP ports for control, ingest, snapshot, RTSP, and ONVIF, and UDP for discovery. Default `false`. |
| `package` | Override the Osprey package. |

The module has no options for MQTT or the
[media persona](protect-dual-adoption.md) yet, and its generated
configuration is read-only, so neither can be enabled through it. Patrols set
up in the console work, because they are saved in the state directory.

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

A VM test that boots the module and checks `/healthz` is planned in
`specs/002-python-package-cli`. It is not in the flake yet.

Keep the flake input pinned with a lock file and review Osprey releases before
updating it. Use your deployment's secret manager for admin, camera, and ONVIF
credentials; never put those values in the flake.
