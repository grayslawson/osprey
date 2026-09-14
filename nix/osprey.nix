{ config, lib, pkgs, ... }:
let
  cfg = config.services.osprey;
  configFile = pkgs.writeText "osprey-config.json" (builtins.toJSON {
    host = cfg.host;
    announce = cfg.announce;
    cert = "${cfg.stateDirectory}/osprey.pem";
    ports = cfg.ports;
  });
in {
  options.services.osprey = {
    enable = lib.mkEnableOption "Osprey PTZ controller";
    package = lib.mkOption { type = lib.types.package; default = pkgs.callPackage ../package.nix {}; description = "Osprey source package."; };
    host = lib.mkOption { type = lib.types.str; description = "LAN address advertised to camera and clients."; };
    announce = lib.mkOption { type = lib.types.bool; default = true; };
    stateDirectory = lib.mkOption { type = lib.types.str; default = "osprey"; readOnly = true; };
    ports = lib.mkOption { default = { control = 7442; ingest = 7550; snapshot = 7444; rtsp = 8554; onvif = 8000; discovery = 3702; }; type = lib.types.attrsOf lib.types.port; };
  };
  config = lib.mkIf cfg.enable {
    assertions = [{ assertion = cfg.host != "0.0.0.0"; message = "services.osprey.host must be a concrete LAN address, not 0.0.0.0"; }];
    systemd.services.osprey = {
      description = "Osprey PTZ controller (PRODUCTION)";
      wantedBy = [ "multi-user.target" ];
      after = [ "network-online.target" ]; wants = [ "network-online.target" ];
      serviceConfig = {
        ExecStart = "${pkgs.python3}/bin/python ${cfg.package}/share/osprey/cuckoo/main.py --config ${configFile}";
        WorkingDirectory = "${cfg.package}/share/osprey/cuckoo";
        StateDirectory = cfg.stateDirectory;
        Restart = "on-failure"; RestartSec = 5;
        DynamicUser = true; NoNewPrivileges = true; PrivateTmp = true;
      };
    };
  };
}
