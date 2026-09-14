# NixOS packaging

`flake.nix` exports `packages.<system>.default` and
`nixosModules.default`. The module runs Osprey as a DynamicUser systemd
service, waits for `network-online.target`, stores its certificate in a private
state directory, and restarts on failure.

Add the flake to a NixOS host configuration:

```nix
{
  inputs.osprey.url = "github:grayslawson/osprey";

  outputs = { self, nixpkgs, osprey, ... }: {
    nixosConfigurations.example = nixpkgs.lib.nixosSystem {
      system = "x86_64-linux";
      modules = [
        osprey.nixosModules.default
        ({ ... }: {
          services.osprey = {
            enable = true;
            host = "192.0.2.10"; # address reachable by the camera and clients
            ports.onvif = 8000;
            ports.rtsp = 8554;
          };
        })
      ];
    };
  };
}
```

`services.osprey.host` must be a concrete LAN address; wildcard binds are
rejected. Open the camera-control, ONVIF, and RTSP ports in your own firewall
policy and ensure the camera can reach the host. The module does not manage
camera credentials, adoption, MQTT secrets, Frigate, or firewall rules.

From the host's own flake (after adding the module above), build and inspect
without contacting a camera:

```sh
nix flake check
nixos-rebuild dry-build --flake .#example
```

Keep the flake input pinned with a lock file and review Osprey releases before
updating it. Use your deployment's secret manager for admin, camera, MQTT, and
ONVIF credentials; never put those values in the flake.
