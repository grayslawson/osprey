// The Assist tab: pending suggestions, per-capability modes, hourly budgets
// and the decision log. Everything goes through the /api/v1/assist/ proxy.
import { useEffect, useState } from "preact/hooks";
import { html } from "./html.js";
import { Badge, Card } from "./common.js";
import { act, errorText } from "../actions.js";
import { ApiError, del, get, pageUrl, post } from "../api.js";

const POLL_MS = 10000;
const MODES = [["off", "Off"], ["observe", "Observe"], ["suggest", "Suggest"], ["act", "Act"], ["auto", "Auto"]];
const CAPABILITIES = [["quality", "Picture quality"], ["survey", "Survey"], ["patrol", "Patrol"], ["followup", "Follow-up"], ["presets", "Presets"]];
const BUDGETS = [["moves", "Camera moves"], ["isp_writes", "Image setting changes"], ["trials", "Trials"], ["model_calls", "Model calls"]];
const PROBLEM = { glare: "Glare", blur: "Blur", under: "Too dark", over: "Too bright", noise: "Noise" };
const REASONS = [["not_a_problem", "Not a problem"], ["wrong_fix", "Wrong fix"], ["looks_worse", "Looks worse"], ["dont_touch", "Do not touch this"]];
const STATUS_TONE = { applied: "warn", kept: "ok", reverted: "bad", failed: "bad", pending: "warn" };
const FILTERS = [["all", "All"], ["pending", "Waiting"], ["applied", "Applied"], ["kept", "Kept"], ["reverted", "Reverted"], ["rejected", "Rejected"]];

const when = (ts) => new Date(ts * 1000).toLocaleString([], { dateStyle: "short", timeStyle: "short" });
const frameUrl = (name) => pageUrl(`api/v1/assist/frames/${encodeURIComponent(name)}.jpg`);
const percent = (value) => `${Math.round(value * 100)}%`;
const label = (value) => (value ? String(value).replace(/_/g, " ") : "");

function GlareGrid({ map }) {
  return html`<div class="assist-grid" aria-hidden="true">
    ${map.map((level, index) => html`<span key=${index} style=${`opacity:${Math.min(level, 9) / 9 * 0.8}`}></span>`)}
  </div>`;
}

function Frame({ name, map, caption }) {
  if (!name) return html`<figure class="assist-frame"><div class="assist-none">No image</div><figcaption>${caption}</figcaption></figure>`;
  return html`<figure class="assist-frame">
    <div class="assist-shot">
      <img src=${frameUrl(name)} alt=${`${caption} frame`} loading="lazy" />
      ${map ? html`<${GlareGrid} map=${map} />` : null}
    </div>
    <figcaption>${caption}</figcaption>
  </figure>`;
}

function Pending({ item, busy, run }) {
  const [grid, setGrid] = useState(false);
  const [rejecting, setRejecting] = useState(false);
  const id = `assist-${item.id}`;
  return html`<article class="assist-item" aria-labelledby=${`${id}-t`}>
    <h3 id=${`${id}-t`}>${PROBLEM[item.problem] || item.problem} at ${item.preset}${item.scene ? `, ${item.scene.toLowerCase()}` : ""}</h3>
    <p class="muted">${when(item.ts)}. Confidence ${percent(item.confidence)} from ${label(item.source)}.</p>
    <div class="assist-frames">
      <${Frame} name=${item.before_frame} map=${grid ? item.glare_map : null} caption="Before" />
      <${Frame} name=${item.after_frame} caption="After" />
    </div>
    ${item.glare_map && item.before_frame ? html`<label class="inline">
      <input type="checkbox" checked=${grid} onChange=${(e) => setGrid(e.currentTarget.checked)} /> Show glare on the before image</label>` : null}
    <p>${item.summary}</p>
    <dl class="kv"><dt>Change</dt><dd class="mono">${item.change}</dd><dt>Expected</dt><dd>${item.expect}</dd></dl>
    ${item.warning ? html`<p class="alert tone-warn" role="note">${item.warning}</p>` : null}
    <div class="row">
      <button type="button" class="primary" disabled=${busy} onClick=${() => run(`decisions/${item.id}/approve`, {}, "Applied.")}>Apply</button>
      <button type="button" aria-expanded=${rejecting} disabled=${busy} onClick=${() => setRejecting(!rejecting)}>Reject</button>
      <button type="button" disabled=${busy} onClick=${() => run(`decisions/${item.id}/snooze`, {}, "Snoozed for 24 hours.")}>Snooze 24 h</button>
    </div>
    ${rejecting ? html`<fieldset class="assist-reasons"><legend>Why reject it?</legend>
      ${REASONS.map(([key, text]) => html`<button type="button" key=${key} disabled=${busy}
        onClick=${() => run(`decisions/${item.id}/reject`, { reason: key }, "Rejected.")}>${text}</button>`)}
    </fieldset>` : null}
  </article>`;
}

function Capability({ id, name, status, busy, setMode }) {
  const mode = status.capabilities && status.capabilities[id] ? status.capabilities[id].mode : "off";
  const ready = (status.implemented || []).includes(id);
  const [asking, setAsking] = useState(false);
  const pick = (value) => (value === "auto" && mode !== "auto" ? setAsking(true) : setMode(id, value));
  return html`<li class="assist-cap">
    <div class="row"><strong id=${`cap-${id}`}>${name}</strong>
      ${ready ? null : html`<span class="muted">Arrives in a later release</span>`}</div>
    <div class="assist-seg" role="radiogroup" aria-labelledby=${`cap-${id}`}>
      ${MODES.map(([value, text]) => html`<button type="button" role="radio" key=${value}
        aria-checked=${mode === value} disabled=${!ready || busy} onClick=${() => pick(value)}>${text}</button>`)}
    </div>
    ${asking ? html`<div class="alert tone-warn" role="alertdialog" aria-label="Confirm Auto mode">
      <p>Auto applies changes without asking you first, including structural ones if that is allowed. You can still undo each change.</p>
      <div class="row">
        <button type="button" class="primary" onClick=${() => { setAsking(false); setMode(id, "auto"); }}>Turn on Auto</button>
        <button type="button" onClick=${() => setAsking(false)}>Cancel</button></div>
    </div>` : null}
  </li>`;
}

function Meter({ name, used, limit }) {
  return html`<div class="assist-meter"><label>${name}: ${used} of ${limit}
    <meter min="0" max=${limit || 1} value=${used}>${used} of ${limit}</meter></label></div>`;
}

function Row({ item, open, toggle, run, busy }) {
  const o = item.outcome;
  const tone = STATUS_TONE[item.status] || "muted";
  return html`<tr>
    <td>${when(item.ts)}</td><td>${item.preset}</td><td>${PROBLEM[item.problem] || item.problem}</td>
    <td>${label(item.source)}</td><td>${item.change}</td>
    <td><${Badge} tone=${tone}>${item.status}</${Badge}>${o ? ` ${o.verdict}` : ""}</td>
    <td><button type="button" aria-expanded=${open} aria-label=${`Details for decision ${item.id}`} onClick=${toggle}>${open ? "Hide" : "Details"}</button>
      ${item.status === "applied" || item.status === "kept"
        ? html` <button type="button" disabled=${busy} onClick=${() => run(`decisions/${item.id}/undo`, {}, "Undone.")}>Undo</button>` : null}</td>
  </tr>${open ? html`<tr><td colspan="7" class="assist-detail">
    <div class="assist-frames"><${Frame} name=${item.before_frame} map=${item.glare_map} caption="Before" /><${Frame} name=${item.after_frame} caption="After" /></div>
    <p>${item.summary} ${item.expect}</p>
    ${item.reason ? html`<p><strong>Reason:</strong> ${item.reason}</p>` : null}
    ${o ? html`<p>Target ${o.target_before} to ${o.target_after}.</p>` : null}
    ${item.metrics ? html`<pre class="mono">${JSON.stringify(item.metrics, null, 2)}</pre>` : null}
    ${item.model_json ? html`<pre class="mono">${JSON.stringify(item.model_json, null, 2)}</pre>` : null}
  </td></tr>` : null}`;
}

export function AssistTab() {
  const [status, setStatus] = useState(null);
  const [down, setDown] = useState(false);
  const [pending, setPending] = useState([]);
  const [log, setLog] = useState([]);
  const [filter, setFilter] = useState("all");
  const [open, setOpen] = useState(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");
  const [forget, setForget] = useState(false);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let stopped = false;
    async function poll() {
      if (document.visibilityState === "hidden") return;
      try {
        const [s, p, l] = await Promise.all([
          get("api/v1/assist/status"),
          get("api/v1/assist/decisions?status=pending&limit=50"),
          get(`api/v1/assist/decisions?status=all&limit=50`),
        ]);
        if (stopped) return;
        setStatus(s); setPending(p.decisions); setLog(l.decisions); setDown(false);
      } catch (err) {
        if (stopped) return;
        if (err instanceof ApiError && err.status !== 502) setNote(errorText(err));
        else setDown(true);
      }
    }
    poll();
    const timer = setInterval(poll, POLL_MS);
    document.addEventListener("visibilitychange", poll);
    return () => { stopped = true; clearInterval(timer); document.removeEventListener("visibilitychange", poll); };
  }, [tick]);

  async function run(path, body, done) {
    setBusy(true);
    const result = await act(post(`api/v1/assist/${path}`, body), done);
    setBusy(false);
    setNote(result ? done : "That did not work. See the message above.");
    if (result) setTick((n) => n + 1);
    return result;
  }
  const setMode = (id, mode) => run("settings", { capabilities: { [id]: { mode } } }, `${label(id)} set to ${mode}.`);

  async function forgetAll() {
    setBusy(true);
    const result = await act(del("api/v1/assist/data"), "Assist forgot everything.");
    setBusy(false);
    setForget(false);
    if (result) { setNote("Assist forgot everything."); setTick((n) => n + 1); }
  }

  if (down) {
    return html`<div class="stack"><p class="alert tone-warn" role="alert">Assist is not answering.</p>
      <div><button type="button" onClick=${() => { setDown(false); setTick((n) => n + 1); }}>Try again</button></div></div>`;
  }
  if (!status) return html`<p class="boot" role="status">Loading Assist…</p>`;

  const shown = log.filter((d) => filter === "all" || d.status === filter);
  const hourly = status.budgets || {};
  return html`<div class="stack">
    <section aria-labelledby="assist-title">
      <div class="card-head">
        <h2 id="assist-title">Assist</h2>
        <div class="row">
          <label class="inline"><input type="checkbox" role="switch" checked=${status.enabled} disabled=${busy}
            onChange=${(e) => run("settings", { enabled: e.currentTarget.checked }, e.currentTarget.checked ? "Assist is on." : "Assist is off.")} />
            Assist ${status.enabled ? "on" : "off"}</label>
          <button type="button" disabled=${busy || !status.enabled} onClick=${() => run("check", {}, "Check queued.")}>Check now</button>
        </div>
      </div>
      ${status.paused ? html`<p class="muted">Assist is paused while the camera is in use.</p>` : null}
      <p class="assist-note" role="status" aria-live="polite">${note}</p>
    </section>

    <${Card} id="assist-waiting" title=${`Waiting for you (${pending.length})`}>
      ${pending.length ? pending.map((item) => html`<${Pending} key=${item.id} item=${item} busy=${busy} run=${run} />`)
        : html`<p class="muted">Nothing is waiting.</p>`}
    </${Card}>

    <${Card} id="assist-caps" title="Capabilities">
      <ul class="assist-caps">${CAPABILITIES.map(([id, name]) => html`<${Capability} key=${id} id=${id} name=${name} status=${status} busy=${busy} setMode=${setMode} />`)}</ul>
    </${Card}>

    <${Card} id="assist-budgets" title="This hour">
      ${BUDGETS.filter(([k]) => hourly[k]).map(([k, name]) => html`<${Meter} key=${k} name=${name} used=${hourly[k].used} limit=${hourly[k].limit} />`)}
      <p class="muted">Model calls today: ${status.models ? status.models.calls_today : 0}${status.models && status.models.left !== null ? `, ${status.models.left} left` : ""}.</p>
    </${Card}>

    <${Card} id="assist-log" title="Decisions"
      actions=${html`<label class="inline">Show <select value=${filter} onChange=${(e) => setFilter(e.currentTarget.value)}>
        ${FILTERS.map(([v, t]) => html`<option value=${v} key=${v}>${t}</option>`)}</select></label>`}>
      ${shown.length ? html`<div class="table-wrap"><table>
        <thead><tr><th>Time</th><th>Preset</th><th>Problem</th><th>Source</th><th>Change</th><th>Status</th><th><span class="sr-only">Actions</span></th></tr></thead>
        <tbody>${shown.map((d) => html`<${Row} key=${d.id} item=${d} open=${open === d.id} busy=${busy} run=${run}
          toggle=${() => setOpen(open === d.id ? null : d.id)} />`)}</tbody></table></div>`
        : html`<p class="muted">No decisions yet.</p>`}
    </${Card}>

    <section aria-labelledby="assist-forget">
      <h2 id="assist-forget">Forget everything</h2>
      <p class="muted">Deletes every saved frame and everything Assist has learned.</p>
      ${forget ? html`<div class="alert tone-bad" role="alertdialog" aria-label="Confirm forget everything"><p>This cannot be undone.</p>
        <div class="row"><button type="button" class="danger" disabled=${busy} onClick=${forgetAll}>Forget everything</button>
        <button type="button" onClick=${() => setForget(false)}>Cancel</button></div></div>`
        : html`<button type="button" class="danger" onClick=${() => setForget(true)}>Forget everything</button>`}
    </section>
  </div>`;
}
