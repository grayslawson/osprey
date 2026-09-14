# NixOS packaging

`flake.nix` exports `packages.<system>.default` and `nixosModules.default`.
The module runs Osprey as a DynamicUser systemd service, waits for
`network-online.target`, stores its certificate in the private state directory,
and restarts on failure. `services.osprey.host` must be the concrete LAN
address reachable by the camera; wildcard binds are rejected.

Example (in a NixOS host configuration):

```nix
inputs.osprey.url = "github:your-org/osprey";
imports = [ inputs.osprey.nixosModules.default ];
services.osprey = {
  enable = true;
  host = "192.0.2.10"; # replace with this host's LAN address
  ports.onvif = 8000;
  ports.rtsp = 8554;
};
```

The module intentionally has no camera credentials, Frigate URL, firewall
opens, or adoption actions. Supply those through the deployment's existing
secret and network policy (in pd-nixos), not the flake or generated config.
Validate with `nix flake check` and `nixos-rebuild dry-build`; neither contacts
the camera.
