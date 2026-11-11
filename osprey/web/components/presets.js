import { useState } from "preact/hooks";
import { html } from "./html.js";
import { Card } from "./common.js";
import { act, refreshState } from "../actions.js";
import { cameraPath, del, pageUrl, post, put } from "../api.js";
import { cam, snapshot } from "../store.js";

export function PresetsPage() {
  const state = snapshot.value;
  const presets = state ? state.presets || [] : [];
  const [name, setName] = useState("");
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const ptzAvailable = !!state && !!state.ptz && state.ptz.supported;

  async function save(event) {
    event.preventDefault();
    const trimmed = name.trim();
    if (!trimmed) {
      setError("Enter a name for this position.");
      return;
    }
    setError("");
    setSaving(true);
    const result = await act(post(cameraPath(cam.value, "/presets"), { name: trimmed }),
      (r) => `Saved ${r.preset ? r.preset.name : trimmed}.`);
    setSaving(false);
    if (result) {
      setName("");
      await refreshState().catch(() => {});
    }
  }

  async function remove(preset) {
    if (!window.confirm(`Delete the preset "${preset.name}"? This cannot be undone.`)) return;
    const result = await act(del(cameraPath(cam.value, `/presets/${preset.token}`)), `Deleted ${preset.name}.`);
    if (result) await refreshState().catch(() => {});
  }

  async function update(preset) {
    const result = await act(put(cameraPath(cam.value, `/presets/${preset.token}`)),
      `Updated ${preset.name} to the current view.`);
    if (result) await refreshState().catch(() => {});
  }

  return html`
    <div class="stack">
      <${Card} id="save-title" title="Save current view">
        <form class="row" onSubmit=${save} noValidate>
          <label>Name
            <input name="preset-name" maxLength="64" value=${name} required
              aria-describedby="preset-error" aria-invalid=${error ? "true" : "false"}
              onInput=${(event) => setName(event.currentTarget.value)} />
          </label>
          <button class="primary" type="submit" disabled=${!ptzAvailable || saving}>
            ${saving ? "Saving…" : "Save position"}</button>
        </form>
        <p id="preset-error" class="field-error" role="alert">${error}</p>
        ${!ptzAvailable ? html`<p class="muted">This camera has no PTZ, so presets are unavailable.</p>` : null}
        <p class="muted">Home is the Frigate return target. Saving a preset named Home replaces it.</p>
      </${Card}>

      <section aria-labelledby="presets-title">
        <h2 id="presets-title">Saved positions</h2>
        ${presets.length === 0
          ? html`<p class="muted">No positions saved yet.</p>`
          : html`<ul class="preset-grid" role="list">
              ${presets.map((preset) => html`
                <li class="preset" key=${preset.token}>
                  <div class="thumb">
                    ${preset.thumbnail
                      ? html`<img alt="" src=${pageUrl(cameraPath(cam.value, `/presets/${preset.token}/thumbnail.jpg`))} />`
                      : html`<span>No thumbnail</span>`}
                  </div>
                  <div class="body">
                    <h3>${preset.name}</h3>
                    ${preset.home ? html`<span class="pill tone-ok">Home · Frigate return target</span>` : null}
                    <span class="muted mono">token ${preset.token}</span>
                  </div>
                  <div class="actions">
                    <button type="button" onClick=${() => act(post(cameraPath(cam.value, `/presets/${preset.token}/goto`)), `Going to ${preset.name}.`)}
                      disabled=${!ptzAvailable}>Go to</button>
                    <button type="button" onClick=${() => update(preset)} disabled=${!ptzAvailable}>Update to current view</button>
                    <button type="button" class="danger" onClick=${() => remove(preset)} disabled=${!ptzAvailable}>Delete</button>
                  </div>
                </li>`)}
            </ul>`}
      </section>
    </div>`;
}
