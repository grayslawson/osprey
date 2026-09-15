# Osprey documentation map

This index is the shortest route into the repository knowledge base. Start here
after reading [`AGENTS.md`](../AGENTS.md), then open only the domain guide that
matches the task.

## Product and architecture

- [Architecture](../cuckoo/ARCHITECTURE.md) — process boundaries, protocol layers,
  and runtime ownership.
- [Design notes](../DESIGN.md) — user-facing design decisions and trade-offs.
- [Multi-camera setup](multi-camera.md) — registering and operating more than one
  camera.

## Install and operate

- [Installation](installation.md) — Docker/Podman, Proxmox, NixOS, Home Assistant,
  updates, rollback, and development setup.
- [Onboarding runbook](onboarding-runbook.md) — first-run setup and recovery.
- [Frigate integration](frigate-integration.md) — connect Osprey to an existing
  Frigate instance.
- [Frigate connection settings](frigate-connection.md) — endpoint and health
  configuration.
- [MQTT](mqtt.md) — optional commands and event publishing.
- [Security](security.md) — trust boundaries, authentication, and exposure rules.
- [Protect dual adoption](protect-dual-adoption.md) — limitations of the
  read-only media persona.

## Development and validation

- [Harnesses](../cuckoo/harness/README.md) — opt-in hardware and client acceptance
  checks.
- [Repository sync](repository-sync.md) — Forgejo ownership, GitHub mirroring, and
  GHCR release responsibilities.
- [NixOS](nixos.md) — declarative package and module usage.
- [Tests](../cuckoo/tests/README.md) — test layout and focused commands.
- [Quality map](quality.md) — domain-by-domain evidence and known limits.

## Maintenance rules

When behavior changes, update the relevant guide and this index if a new domain
or entry point is introduced. Keep private addresses, credentials, recordings,
and deployment state out of all documentation. If a guide is provisional, label
the limitation and verification date in that guide rather than relying on chat
history.
