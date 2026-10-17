# NixOS module: services.osprey
#
# Runs the controller as a DynamicUser systemd service. Everything it writes
# goes to StateDirectory (/var/lib/osprey): the certificate, the first-run
# wizard overlay (osprey-secrets.env, frigate.json). The generated config is a
# read-only file in the Nix store, and secrets are read from files through
# LoadCredential, so they never enter the store.
{ config, lib, pkgs, ... }:
let
  cfg = config.services.osprey;
  statePath = "/var/lib/${cfg.stateDirectory}";
  configFile = pkgs.writeText "osprey-config.json" (builtins.toJSON {
    host = cfg.host;
    announce = cfg.announce;
    cert = "${statePath}/osprey.pem";
    ports = cfg.ports;
  });

  # Secret files, keyed by systemd credential name. The environment variable
  # names are the ones the controller reads (cuckoo/onvif.py, cuckoo/main.py).
  secrets = {
    admin-password = { file = cfg.adminPasswordFile; env = "OSPREY_ADMIN_PASSWORD_FILE"; };
    onvif-password = { file = cfg.onvifPasswordFile; env = "OSPREY_ONVIF_PASSWORD_FILE"; };
    rtsp-password = { file = cfg.rtspPasswordFile; env = "OSPREY_RTSP_PASSWORD_FILE"; };
  };
  activeSecrets = lib.filterAttrs (_: s: s.file != null) secrets;
in
{
  options.services.osprey = {
    enable = lib.mkEnableOption "the Osprey PTZ controller";

    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.callPackage ../package.nix { };
      defaultText = lib.literalExpression "pkgs.callPackage ./package.nix { }";
      description = "Osprey package. Its `osprey` binary is the service entry point.";
    };

    host = lib.mkOption {
      type = lib.types.str;
      example = "192.0.2.10";
      description = "Concrete LAN address the camera and clients reach Osprey on. Never a wildcard or bridge address.";
    };

    announce = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Answer and send WS-Discovery Hello on UDP 3702.";
    };

    stateDirectory = lib.mkOption {
      type = lib.types.str;
      default = "osprey";
      readOnly = true;
      description = "Name under /var/lib that systemd creates and owns for Osprey state.";
    };

    ports = lib.mkOption {
      type = lib.types.submodule {
        options = lib.mapAttrs (_: d: lib.mkOption { type = lib.types.port; default = d; }) {
          control = 7442;
          ingest = 7550;
          snapshot = 7444;
          rtsp = 8554;
          onvif = 8000;
          discovery = 3702;
        };
      };
      default = { };
      description = "Listener ports. control/ingest/snapshot are used by the camera; rtsp/onvif by clients; discovery is UDP.";
    };

    adminPasswordFile = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      example = lib.literalExpression "config.sops.secrets.osprey-admin.path";
      description = "File containing the web UI admin password. Loaded with LoadCredential; not copied to the store.";
    };

    onvifPasswordFile = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      description = "File containing the ONVIF client password (used with OSPREY_ONVIF_USERNAME set in the state env file).";
    };

    rtspPasswordFile = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      description = "File containing the RTSP digest password.";
    };

    openFirewall = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = ''
        Open the TCP ports for camera control, snapshot, ingest, ONVIF and RTSP,
        and UDP for WS-Discovery, in the NixOS firewall. Leave false if your
        firewall policy is managed elsewhere.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = !(builtins.elem cfg.host [ "0.0.0.0" "::" "" ]);
        message = "services.osprey.host must be a concrete LAN address, not a wildcard.";
      }
    ];

    networking.firewall = lib.mkIf cfg.openFirewall {
      allowedTCPPorts = [ cfg.ports.control cfg.ports.ingest cfg.ports.snapshot cfg.ports.rtsp cfg.ports.onvif ];
      allowedUDPPorts = [ cfg.ports.discovery ];
    };

    systemd.services.osprey = {
      description = "Osprey UniFi PTZ controller";
      wantedBy = [ "multi-user.target" ];
      wants = [ "network-online.target" ];
      after = [ "network-online.target" ];
      restartTriggers = [ configFile ];

      environment = {
        OSPREY_CONFIG_FILE = "${configFile}";
        OSPREY_STATE_DIR = statePath;
        OSPREY_SECRETS_FILE = "${statePath}/osprey-secrets.env";
        OSPREY_FRIGATE_CONFIG_FILE = "${statePath}/frigate.json";
      } // lib.mapAttrs' (name: s: lib.nameValuePair s.env "%d/${name}") activeSecrets;

      serviceConfig = {
        # The wrapper puts openssl (certificate generation) and ffmpeg (preview)
        # on PATH and sets PYTHONPATH, so nothing here depends on the host PATH.
        ExecStart = "${lib.getExe cfg.package} --config ${configFile} --cert ${statePath}/osprey.pem";
        WorkingDirectory = statePath;
        DynamicUser = true;
        StateDirectory = cfg.stateDirectory;
        StateDirectoryMode = "0750";
        UMask = "0077";
        LoadCredential = lib.mapAttrsToList (name: s: "${name}:${toString s.file}") activeSecrets;

        Restart = "on-failure";
        RestartSec = 5;

        CapabilityBoundingSet = "";
        AmbientCapabilities = "";
        NoNewPrivileges = true;
        PrivateTmp = true;
        PrivateDevices = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        ProtectKernelTunables = true;
        ProtectKernelModules = true;
        ProtectKernelLogs = true;
        ProtectControlGroups = true;
        ProtectClock = true;
        ProtectHostname = true;
        ProtectProc = "invisible";
        ProcSubset = "pid";
        RestrictAddressFamilies = [ "AF_UNIX" "AF_INET" "AF_INET6" "AF_NETLINK" ];
        RestrictNamespaces = true;
        RestrictRealtime = true;
        RestrictSUIDSGID = true;
        LockPersonality = true;
        MemoryDenyWriteExecute = true;
        SystemCallArchitectures = "native";
        SystemCallFilter = [ "@system-service" "~@privileged" ];
      };
    };
  };
}
