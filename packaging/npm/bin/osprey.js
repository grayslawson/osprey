#!/usr/bin/env node
const { spawnSync } = require("node:child_process");
const args = process.argv.slice(2);
const runtime = process.env.OSPREY_RUNTIME || "docker";
const image = process.env.OSPREY_IMAGE || "ghcr.io/grayslawson/osprey:latest";
if (args[0] === "doctor") {
  const result = spawnSync(runtime, ["run", "--rm", "--network", "host", image, "/usr/local/bin/osprey", "doctor", ...args.slice(1)], { stdio: "inherit" });
  process.exit(result.status ?? 1);
}
console.log("Osprey uses compose.release.yaml for deployment. Run: osprey doctor");
