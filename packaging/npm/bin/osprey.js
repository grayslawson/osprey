#!/usr/bin/env node
const { spawnSync } = require("node:child_process");
const args = process.argv.slice(2);
const runtime = process.env.OSPREY_RUNTIME || (spawnSync("docker", ["--version"]).status === 0 ? "docker" : "podman");
if (args[0] === "doctor") {
  const result = spawnSync(runtime, ["run", "--rm", "--network", "host", "ghcr.io/grayslawson/osprey:latest", "python3", "main.py", "--help"], { stdio: "inherit" });
  process.exit(result.status ?? 1);
}
if (!args.length || args[0] === "--help" || args[0] === "-h") {
  console.log("Usage: osprey doctor\n\nSet OSPREY_RUNTIME=docker or podman. For the controller, use compose.release.yaml.");
  process.exit(0);
}
console.error("The npm package is a launcher; run the documented Compose deployment for Osprey.");
process.exit(2);
