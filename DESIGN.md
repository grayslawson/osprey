---
version: alpha
name: "Cuckoo operator surface"
description: "A dense, calm local control surface for supervising one camera without obscuring live operational state."
colors:
  background: "#f6f7f9"
  panel: "#ffffff"
  foreground: "#1c2024"
  muted: "#6b7280"
  line: "#e4e7eb"
  accent: "#1f6feb"
  success: "#1a7f37"
  warning: "#bf8700"
typography:
  sans:
    fontFamily: "-apple-system, Segoe UI, Roboto, sans-serif"
  mono:
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace"
rounded:
  sm: "0.375rem"
  panel: "0.625rem"
  pill: "999px"
spacing:
  page-padding: "1rem"
  panel-padding: "1rem"
  panel-gap: "1rem"
  page-max: "60rem"
components:
  telemetry-card: {}
  status-badge: {}
  control-button: {}
---

# Cuckoo operator surface

## Overview

### Creative North Star

The UI should feel like a well-labelled piece of field equipment: current state is legible at a glance, controls are close to the evidence they affect, and decoration never competes with an operator’s next action.

### Product context

- **Audience and job:** A trusted local operator supervises an adopted Cuckoo camera, checks its streams, frames its PTZ home view, and makes limited supported configuration changes.
- **Usage scene:** Desktop or tablet on the same trusted network, occasionally under time pressure. The page is a compact single-camera dashboard, not a public marketing surface or a general camera-management product.
- **Register:** Product/operator. Dense cards and technical details are appropriate; buttons use plain verbs and state messages explain what failed.
- **Signature:** Telemetry and control live in the same restrained slate-and-blue instrument panel, with a snapshot as the primary visual anchor.
- **Restraint:** Do not introduce decorative hero art, gradients, dashboard widgets without real data, or controls for unsupported camera settings.
- **Security boundary:** The current telemetry page is a trusted-network lab/operator surface. A release UI must be served separately behind authenticated application routes; do not represent this page as an authenticated admin console.
- **Token ownership/runtime mapping:** `cuckoo/onvif.py` owns the current inline CSS variables. The frontmatter records those tokens so a later shared web application can migrate them deliberately rather than copy values ad hoc.

## Colors

Light mode uses `background`, `panel`, `foreground`, `muted`, and `line` for quiet, high-contrast operational reading. `accent` is reserved for actionable technical values and graph traces; `success` and `warning` communicate stream/adoption state, never status alone. Dark mode preserves the same semantic roles with the existing `prefers-color-scheme` values in `cuckoo/onvif.py`.

## Typography

Use the system sans stack for compact controls and concise status text. Use `mono` only for addresses, stream URIs, and exact technical identifiers. Headings are small uppercase labels with tracking; do not use display typography.

## Layout

The page is centered at `page-max` with `page-padding`. Cards form an auto-fitting grid with `panel-gap`; the streams table and endpoints can span the grid. The snapshot reserves its natural media region and controls wrap rather than overflow at narrow widths. The stream table owns horizontal scrolling; the page retains normal document scrolling.

## Elevation & depth

Use panel color and `line` borders for hierarchy, not heavy shadows. Status dots and badges provide the only high-saturation semantic accents. No floating controls or overlays are part of the current surface.

## Shapes

Cards use `panel`, buttons and selects use `sm`, and compact status badges use `pill`. Keep borders thin and consistent with `line`.

## Components

### Foundational visual states

Native buttons and selects must retain visible keyboard focus. A request reports progress and outcome through the persistent polite status region without moving surrounding controls. Disabled or unavailable controls must state why they cannot operate.

### Buttons and actions

Controls use visible text labels: Pan, Tilt, Zoom, and Save home. A move is a small, bounded step. Saving Home is only available after the PTZ state is idle and always replaces the named preset token rather than creating an ambiguous duplicate.

### Navigation and data display

The surface has no navigation shell. Tables favor compact technical facts, right-aligned tabular numbers, and responsive horizontal scrolling for the wide streams table.

### Forms and overlays

Codec selectors are native selects: their platform-owned popup behaviour is accepted for this small trusted operator surface. JSON control requests validate server-side and surface a specific response in the live region. No browser dialogs are used.

### Motion

No decorative animation. The auto-refresh and state feedback communicate fresh camera state; respect reduced-motion preferences if future transitions are introduced.

### Content and data visualization

Use literal operational vocabulary: “PTZ channel unavailable”, “wait for movement to finish”, and codec names. Rates use compact technical units and spark lines supplement—not replace—the tabular values.

## Do's and Don'ts

- **Do:** Keep every displayed control backed by a real controller capability.
- **Do:** Keep stream, PTZ, and preset state near the action that depends on it.
- **Don't:** Add a browser-playable audio claim when the browser cannot play the RTSP audio stream.
- **Don't:** Treat the lab telemetry page as a substitute for the authenticated release application.
