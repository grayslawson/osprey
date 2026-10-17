import { html } from "./html.js";
import { toasts } from "../store.js";

export const TONE_LABEL = { ok: "OK", warn: "Warning", bad: "Problem", muted: "Unknown" };

// Status is always shown as text next to colour (WCAG 1.4.1).
export function Badge({ tone = "muted", children }) {
  return html`<span class="pill tone-${tone}"><span class="dot" aria-hidden="true"></span>${children}</span>`;
}

export function toneForPtz(status) {
  if (status === "UNKNOWN") return "bad";
  if (status === "MOVING") return "warn";
  if (status === "IDLE") return "ok";
  return "muted";
}

export const PTZ_LABEL = {
  IDLE: "Idle",
  MOVING: "Moving",
  UNKNOWN: "Unknown",
  UNAVAILABLE: "Not available",
};

export function Card({ title, actions, children, id }) {
  return html`
    <section class="card" aria-labelledby=${id}>
      <div class="card-head">
        <h2 id=${id}>${title}</h2>
        ${actions}
      </div>
      ${children}
    </section>`;
}

export function Toasts() {
  return html`
    <div class="toasts" role="status" aria-live="polite">
      ${toasts.value.map((t) => html`<div class="toast tone-${t.tone}" key=${t.id}>${t.message}</div>`)}
    </div>`;
}

export function Loading({ children }) {
  return html`<p class="boot" role="status">${children || "Loading…"}</p>`;
}
