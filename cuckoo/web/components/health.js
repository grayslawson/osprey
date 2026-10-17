import { html } from "./html.js";
import { Badge } from "./common.js";
import { act } from "../actions.js";
import { cameraPath, post } from "../api.js";
import { cam, snapshot } from "../store.js";

const TONE = { ok: "ok", warn: "warn", bad: "bad" };
const STATUS = { ok: "OK", warn: "Warning", bad: "Problem" };
const ACTIONS = {
  recover: ["Recover PTZ", (c) => post(cameraPath(c, "/ptz/recover"))],
  home: ["Go to Home", (c) => post(cameraPath(c, "/ptz/home"))],
};

export function HealthPage() {
  const state = snapshot.value;
  const checks = state && state.health ? state.health.checks || [] : [];
  const problems = checks.filter((c) => c.status === "bad").length;
  const warnings = checks.filter((c) => c.status === "warn").length;
  return html`
    <section aria-labelledby="health-title">
      <h2 id="health-title">Health</h2>
      <p class="muted">${problems} problem(s) and ${warnings} warning(s). Each check says what it means and what to do.</p>
      ${checks.length === 0
        ? html`<p class="muted">No checks yet.</p>`
        : html`<ul class="checks card">
            ${checks.map((check) => {
              const tone = TONE[check.status] || "warn";
              return html`<li key=${check.id}>
                <div class="row">
                  <strong>${check.label}</strong>
                  <${Badge} tone=${tone}>${STATUS[check.status] || check.status}</${Badge}>
                </div>
                <span>${check.detail}</span>
                ${(check.actions || []).length
                  ? html`<div class="row">${check.actions.filter((a) => ACTIONS[a]).map((action) => html`
                      <button type="button" key=${action}
                        onClick=${() => act(ACTIONS[action][1](cam.value), (r) => r.message || `${ACTIONS[action][0]} requested.`)}>
                        ${ACTIONS[action][0]}</button>`)}</div>`
                  : null}
              </li>`;
            })}
          </ul>`}
    </section>`;
}
