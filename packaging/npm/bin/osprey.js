#!/usr/bin/env node

const { spawnSync } = require("node:child_process");

const args = process.argv.slice(2);
const runtime = process.env.OSPREY_RUNTIME || "docker";
const image = process.env.OSPREY_IMAGE || "ghcr.io/grayslawson/osprey:0.9.0b7";

// The release image exposes the controller entrypoint, not a bare `osprey`
// binary. Read-only commands are run through the same entrypoint so they see
// the packaged scripts and state layout.
const ENTRYPOINT = "/usr/local/bin/osprey-entrypoint";
const READ_ONLY_COMMANDS = new Set(["doctor", "probe", "protect-guard", "version"]);

function usage() {
  console.log(`Usage: osprey <command> [options]

Commands:
  doctor          Check the host, ports, and Frigate reachability
  probe           Write a redacted data bundle for an unsupported camera model
  protect-guard   Check or create the UniFi firewall policy that keeps
                  Protect from adopting the physical camera
  version         Print the packaged Osprey version
  help            Show this help

The npm package is a thin container launcher. Set OSPREY_RUNTIME to docker or
podman and OSPREY_IMAGE to a release tag or digest. Use compose.release.yaml
for the controller deployment.`);
}

if (args.length === 0 || args[0] === "help" || args[0] === "--help" || args[0] === "-h") {
  usage();
  process.exit(0);
}

if (READ_ONLY_COMMANDS.has(args[0])) {
  const result = spawnSync(
    runtime,
    ["run", "--rm", "--network", "host", image, ENTRYPOINT, ...args],
    { stdio: "inherit" },
  );
  if (result.error) {
    console.error(`Unable to run ${runtime}: ${result.error.message}`);
    process.exit(1);
  }
  process.exit(result.status ?? 1);
}

console.error(`Unknown command: ${args[0]}`);
usage();
process.exit(2);
