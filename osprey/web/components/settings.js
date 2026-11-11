import { useState } from "preact/hooks";
import { html } from "./html.js";
import { Card } from "./common.js";
import { act, refreshState } from "../actions.js";
import { cameraPath, put } from "../api.js";
import { cam, session, snapshot } from "../store.js";

const CODECS = ["h264", "h265", "mjpg"];

function FrigateForm({ frigate }) {
  const [url, setUrl] = useState(frigate && frigate.base_url ? frigate.base_url : "");
  const [saving, setSaving] = useState(false);

  async function save(event) {
    event.preventDefault();
    setSaving(true);
    const result = await act(put(cameraPath(cam.value, "/frigate"), { base_url: url.trim() || null }),
      url.trim() ? "Frigate address saved." : "Frigate address cleared.");
    setSaving(false);
    if (result) await refreshState().catch(() => {});
  }

  return html`
    <form class="stack" onSubmit=${save} noValidate>
      <label>Frigate base address
        <input type="url" name="frigate-url" value=${url} placeholder="http://frigate.local:5000"
          onInput=${(event) => setUrl(event.currentTarget.value)} />
      </label>
      <p class="muted">${frigate && frigate.configured ? "Configured." : "Not configured. Frigate autotracking needs this address."}</p>
      <div><button class="primary" type="submit" disabled=${saving}>${saving ? "Saving…" : "Save Frigate address"}</button></div>
    </form>`;
}

function CodecSelect({ track }) {
  async function change(event) {
    const codec = event.currentTarget.value;
    const confirmed = window.confirm(
      `Change ${track.name} to ${codec}? Clients connected to this stream may reconnect.`);
    if (!confirmed) {
      event.currentTarget.value = track.codec;
      return;
    }
    const result = await act(put(cameraPath(cam.value, `/tracks/${encodeURIComponent(track.name)}`), { codec }),
      `${track.name} is now ${codec}.`);
    if (result) await refreshState().catch(() => {});
    else event.currentTarget.value = track.codec;
  }
  return html`
    <label>${track.name} codec
      <select value=${track.codec} onChange=${change}>
        ${CODECS.map((codec) => html`<option value=${codec} key=${codec}>${codec}</option>`)}
      </select>
    </label>`;
}

export function SettingsPage() {
  const state = snapshot.value;
  const value = session.value;
  const camera = state ? state.camera : null;
  return html`
    <div class="grid">
      <${Card} id="camera-title" title="Camera">
        ${camera
          ? html`<dl class="kv">
              <dt>Name</dt><dd>${camera.name}</dd>
              <dt>Model</dt><dd>${camera.model || "unknown"}</dd>
              <dt>Firmware</dt><dd>${camera.firmware || "unknown"}</dd>
              <dt>Adopted</dt><dd>${camera.adopted ? "yes" : "no"}</dd>
            </dl>`
          : html`<p class="muted">No camera is connected yet.</p>`}
      </${Card}>

      <${Card} id="tracks-title" title="Video tracks"
        actions=${html`<span class="muted">Changes ask for confirmation</span>`}>
        ${camera && camera.tracks && camera.tracks.length
          ? html`<div class="stack">${camera.tracks.map((track) => html`<${CodecSelect} track=${track} key=${track.name} />`)}</div>`
          : html`<p class="muted">No tracks reported.</p>`}
      </${Card}>

      <${Card} id="frigate-title" title="Frigate">
        <${FrigateForm} frigate=${state ? state.frigate : null} />
      </${Card}>

      <${Card} id="access-title" title="Access">
        <p>Sign-in mode: <span class="mono">${value ? value.mode : "unknown"}</span></p>
        <p class="muted">Sign out from the header when you use a shared browser.</p>
      </${Card}>
    </div>`;
}
