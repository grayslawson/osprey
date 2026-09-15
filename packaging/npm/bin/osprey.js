#!/usr/bin/env node
const { spawnSync } = require("node:child_process");
const args = process.argv.slice(2);
const runtime = process.env.OSPREY_RUNTIME || (spawnSync("docker", ["--version"]).status === 0 ? "docker" : "podman");
const image = process.env.OSPREY_IMAGE || "ghcr.io/grayslawson/osprey:latest";
if (args[0] === "doctor") {
 const result = spawnSync(runtime, ["run", "--rm", "--network", "host", image, "/usr/local/bin/osprey", "doctor", ...args.slice(1)], { stdio: "inherit" });
  process.exit(result.status ?? 1);
}
if (!args.length || args[0] === "--help" || args[0] === "-h") {
  console.log("Usage: osprey doctor\n\nSet OSPREY_RUNTIME=docker or podman. For the controller, use compose.release.yaml.");
  process.exit(0);
}
console.error("The npm package provides the doctor launcher; use compose.release.yaml to run Osprey.");
process.exit(2);
