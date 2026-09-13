<p align="center">
  <img src="assets/osprey-header.png" alt="An osprey in flight" width="900">
</p>

<h1 align="center">Osprey</h1>

<p align="center">
  A local-first PTZ camera system that gives Frigate a calmer, more capable set of eyes.
</p>

<p align="center">
  <img alt="Stage: alpha" src="https://img.shields.io/badge/stage-alpha-b7791f?style=flat-square">
  <img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-2f6f54?style=flat-square">
  <img alt="Local-first" src="https://img.shields.io/badge/video-local--first-304b61?style=flat-square">
</p>

Osprey brings camera control, saved views, live feedback, and Frigate
autotracking together for a UniFi G5 PTZ. It is for people who want useful
automation without handing their camera feed to a cloud service.

> [!WARNING]
> Osprey is alpha software. The current physical deployment has no user
> accounts or authentication. Keep it on a trusted LAN, do not forward its
> ports through a router, and supervise physical camera setup.

## Why Osprey

Your PTZ camera should not feel like a separate appliance you only touch when
something goes wrong. Osprey gives it a home position, named views, a real-time
operator surface, and a path to measured, scene-aware tracking.

| See clearly | Move deliberately | Stay in control |
| --- | --- | --- |
| Live browser preview, stream health, frame rate, and bandwidth at a glance. | Coarse or fine pan/tilt steps, a 0–100% zoom target, Home, and named positions. | Local video, local control, explicit failures, and no cloud account requirement. |

## What works today

- **Operator console** — live preview with snapshot fallback, PTZ controls,
  telemetry, zoom, codec controls, Home, and named positions.
- **Frigate integration** — local detection, recording, review, and PTZ
  autotracking for people and cars. The physical profile also configures birds,
  cats, and dogs.
- **Distance-aware zoom** — conservative absolute zoom avoids the rapid
  oscillation seen in early relative-zoom testing.
- **Safety checks** — a fast-target acceptance tool measures detection-to-command
  delay and predicted-versus-observed motor timing before revised settings are
  accepted.
- **Home Assistant direction** — an alpha add-on scaffold is included for
  contributors and early testers.

## What we are finishing next

| In progress | Why it matters |
| --- | --- |
| Fast-vehicle acceptance | A camera should not chase where a car used to be. Osprey is validating timing before claiming that result. |
| Sentry patrol | Rotate through saved views only while tracking is idle, without competing with a tracked subject. |
| Home Assistant release | Publish and validate add-on images before asking anyone to install from the add-on store. |
| Authentication and roles | Required before Osprey can safely become a shared or remotely reachable service. |

## Get started

Osprey currently runs from this repository with Docker Compose.

```bash
git clone https://github.com/grayslawson/osprey.git
cd osprey
docker compose build cuckoo
docker compose up -d --wait --wait-timeout 120 cuckoo finch
docker compose --profile test run --rm integration-test
```

When the verification step completes, open the local operator page:

```text
http://127.0.0.1:18000/
```

The supported path today is Linux amd64 with Docker Compose. Rootless Podman
works for the local development path. Windows with Docker Desktop is supported
for local development; physical camera networking needs the Windows relay path.
Apple Silicon and other architectures have not been validated yet.

## Add Frigate

Frigate is intentionally kept separate so you keep control of storage,
detectors, and upgrade timing. Start it after the controller stack is healthy:

```bash
docker compose --profile frigate run --rm frigate-init
docker compose --profile frigate up -d --wait frigate
```

Its local UI is available at `http://127.0.0.1:15000/`. Keep it loopback-only
until you have a deliberate, authenticated network boundary.

## Home Assistant

The add-on source is in [homeassistant/](homeassistant/README.md). It uses Home
Assistant ingress for the console and host networking for camera callbacks.
It is not yet a store-ready add-on: published images and validation on supported
Home Assistant installations are still required.

## Privacy and safety

- Osprey is designed to keep video and control traffic on your network.
- Do not commit camera addresses, MAC addresses, passwords, API keys, or video
  clips. Use the provided example configuration as a template only.
- Do not reset, unmanage, or re-adopt a working camera as a routine setup step.
- Back up Frigate configuration and reviewed movement settings before changing
  versions or recalibrating the camera.

## Built on Cuckoo and Finch

Osprey is built on the work of the original
[Cuckoo](https://github.com/rjmotion/cuckoo),
[Finch](https://github.com/rjmotion/finch), and
[pyunifiwire](https://github.com/rjmotion/pyunifiwire) authors and contributors.
Their camera protocol and runtime work is the foundation of this project.

## Contributing

Osprey is early and practical feedback is valuable. Please include the Osprey
version, host platform, camera model, Frigate version, and a redacted log or
reproducible description when reporting an issue. Never include credentials,
private addresses, MAC addresses, or video from someone else's property.

## License

Osprey is available under the [MIT License](LICENSE). Upstream components keep
their own license notices.
