#!/usr/bin/env node

const { spawnSync } = require("node:child_process");

const args = process.argv.slice(2);
const runtime = process.env.OSPREY_RUNTIME || "docker";
const image = process.env.OSPREY_IMAGE || "ghcr.io/grayslawson/osprey:0.1.0";

function usage() {
  console.log(`Usage: osprey <command> [options]

Commands:
  doctor       Check the host, ports, and Frigate reachability
  help         Show this help

The npm package is a thin container launcher. Set OSPREY_RUNTIME to docker or
podman and OSPREY_IMAGE to a release tag or digest. Use compose.release.yaml
for the controller deployment.`);
}

if (args.length === 0 || args[0] === "help" || args[0] === "--help" || args[0] === "-h") {
  usage();
  process.exit(0);
}

if (args[0] === "doctor") {
  const result = spawnSync(
    runtime,
    ["run", "--rm", "--network", "host", image, "/usr/local/bin/osprey", "doctor", ...args.slice(1)],
    { stdio: "inherit" },
  );
  if (result.error) {
    console.error(`Unable to run ${runtime}: ${result.error.message}`);
  }
  process.exit(result.status ?? 1);
}

console.error(`Unknown command: ${args[0]}`);
usage();
process.exit(2);
