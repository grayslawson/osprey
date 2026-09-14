{
  description = "Osprey UniFi G5 PTZ ONVIF/RTSP controller";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";
  outputs = { self, nixpkgs }:
    let systems = [ "x86_64-linux" "aarch64-linux" ];
        eachSystem = f: nixpkgs.lib.genAttrs systems (system:
          f (import nixpkgs { inherit system; }));
    in {
      packages = eachSystem (pkgs: {
        default = pkgs.callPackage ./package.nix {};
      });
      nixosModules.default = import ./nix/osprey.nix;
    };
}
