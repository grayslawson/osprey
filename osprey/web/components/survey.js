// The Survey tab: the panorama with labelled regions, proposed presets as pins and the patrol path.
// Everything goes through the /api/v1/assist/survey/ proxy.
import { useEffect, useState } from "preact/hooks";
import { html } from "./html.js";
import { Badge, Card } from "./common.js";
import { act, errorText } from "../actions.js";
import { ApiError, get, pageUrl, post } from "../api.js";
import { cam } from "../store.js";

const BASE = "api/v1/assist/survey";
const FAST_MS = 3000;
const SLOW_MS = 10000;
const MODES = [["off", "Off"], ["observe", "Observe"], ["suggest", "Suggest"], ["act", "Act"], ["auto", "Auto"]];
const COLORS = {
  door: "#e6503c", gate: "#f09628", driveway: "#788296", path: "#c8aa6e", street: "#505064", yard: "#6ebe5a",
  fence_line: "#be78c8", sky: "#5aaaf0", wall_eaves: "#a0a096", foliage: "#1e823c", glare_source: "#fff050", other: "#969696",
};
const LABEL = {
  door: "Door", gate: "Gate", driveway: "Driveway", path: "Path", street: "Street", yard: "Yard", fence_line: "Fence line",
  sky: "Sky", wall_eaves: "Wall or eaves", foliage: "Foliage", glare_source: "Glare", other: "Other",
};
const STATE = { ready: "ok", partial: "warn", running: "warn", paused: "warn", analysing: "warn", queued: "warn", interrupted: "warn", failed: "bad", cancelled: "muted" };
const PROP_TONE = { pending: "warn", applied: "ok", rejected: "muted", failed: "bad", reverted: "muted", expired: "muted", observed: "muted", written: "ok" };

const when = (ts) => new Date(ts * 1000).toLocaleString([], { dateStyle: "short", timeStyle: "short" });
const q = (cam_id, extra = "") => `?camera=${encodeURIComponent(cam_id)}${extra}`;
const asset = (name, cam_id, kind, v) => pageUrl(`${BASE}/${name}${q(cam_id, `&kind=${kind}&v=${v}`)}`);

function place(ext, az, el) {
  const [az0, el0, az1, el1] = ext;
  return { left: `${((az - az0) / (az1 - az0)) * 100}%`, top: `${((el1 - el) / (el1 - el0)) * 100}%` };
}

function azel(geo, p) {
  if (p.az !== undefined) return [p.az, p.el];
  return [(p.pan * geo.pan_deg) / 2, (p.tilt * geo.tilt_deg) / 2];
}

function Progress({ run }) {
  if (!run) return null;
  const total = run.total || 0;
  const done = Math.min(run.done || 0, total);
  const open = ["queued", "running", "paused", "analysing"].includes(run.status);
  return html`<div class="survey-progress" role="status" aria-live="polite">
    <div class="row"><${Badge} tone=${STATE[run.status] || "muted"}>${run.status}</${Badge}>
      <span>${run.kind === "night" ? "Night" : "Day"} survey, ${when(run.ts)}${open && run.phase ? `. ${run.phase}` : ""}</span></div>
    ${total ? html`<label class="assist-meter">${done} of ${total} pictures<meter min="0" max=${total} value=${done}>${done} of ${total}</meter></label>` : null}
    ${run.reason ? html`<p class="muted">${run.reason}</p>` : null}
  </div>`;
}

function Detail({ item, geo, map, camId, busy, run, onClose }) {
  const preview = pageUrl(`${BASE}/proposals/${item.id}/preview.jpg`);
  const can = item.status === "pending";
  return html`<aside class="survey-detail card" aria-label=${`Proposal ${item.name || item.type}`}>
    <div class="card-head"><h3>${item.name || (item.type === "route" ? "Patrol route" : item.type)}</h3>
      <button type="button" onClick=${onClose} aria-label="Close details">Close</button></div>
    <p><${Badge} tone=${PROP_TONE[item.status] || "muted"}>${item.status}</${Badge}> ${item.reason || ""}</p>
    ${item.type === "preset" || item.type === "night" ? html`
      <figure class="assist-frame"><div class="assist-shot"><img src=${preview} alt=${`Preview of ${item.name}`} loading="lazy" /></div>
        <figcaption>What the panorama shows there. The camera may look a little different.</figcaption></figure>
      ${item.label ? html`<p>Interest ${Math.round((item.interest || 0) * 100)}%, zoom ${Math.round((item.zoom || 0) * 100)}%.</p>` : null}` : null}
    ${item.type === "night" ? html`<p>Glare drops from ${Math.round(item.glare_before * 100)}% to ${Math.round(item.glare_after * 100)}% of the picture. It is checked with a real picture before it is used.</p>` : null}
    ${item.type === "route" ? html`<ol class="survey-route">${item.points.map((p) => html`<li key=${p.name}>${p.name}${p.dwell_sec ? `, ${Math.round(p.dwell_sec)} s` : ""}${(item.new || []).includes(p.name) ? " (new)" : ""}</li>`)}</ol>
      <p class="muted">${item.policy === "merge" ? "Your own stops keep their order. New ones go where they add the least travel." : "This replaces the whole route."}${item.enable ? " The patrol will be switched on." : ""}</p>` : null}
    <div class="row">
      ${(item.type === "preset" || item.type === "night") ? html`<button type="button" disabled=${busy || run} onClick=${() => post(`${BASE}/proposals/${item.id}/goto`, {}).catch(() => {})}>Go there</button>` : null}
      ${can ? html`<button type="button" class="primary" disabled=${busy} onClick=${() => onClose("accept")}>Accept</button>
        <button type="button" disabled=${busy} onClick=${() => onClose("reject")}>Reject</button>` : null}
    </div>
  </aside>`;
}

export function SurveyTab() {
  const camId = cam.value || "default";
  const [status, setStatus] = useState(null);
  const [down, setDown] = useState(false);
  const [kind, setKind] = useState("day");
  const [map, setMap] = useState(null);
  const [noMap, setNoMap] = useState(false);
  const [view, setView] = useState({ labels: true, pins: true, path: true, changes: false });
  const [open, setOpen] = useState(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");
  const [tick, setTick] = useState(0);
  const running = !!(status && status.busy);

  useEffect(() => {
    let stopped = false;
    let timer = 0;
    async function poll() {
      if (document.visibilityState === "hidden") return schedule();
      try {
        const s = await get(`${BASE}/status${q(camId)}`);
        if (stopped) return;
        setStatus(s);
        setDown(false);
      } catch (err) {
        if (stopped) return;
        if (err instanceof ApiError && err.status !== 502) setNote(errorText(err));
        else setDown(true);
      }
      schedule();
    }
    function schedule() {
      if (!stopped) timer = setTimeout(poll, running ? FAST_MS : SLOW_MS);
    }
    poll();
    return () => { stopped = true; clearTimeout(timer); };
  }, [tick, camId, running]);

  const stamp = status && status.maps && status.maps[kind] ? status.maps[kind].id : 0;
  useEffect(() => {
    if (!status) return;
    if (!status.maps || !status.maps[kind]) { setMap(null); setNoMap(true); return; }
    let stopped = false;
    get(`${BASE}/map${q(camId, `&kind=${kind}`)}`)
      .then((m) => { if (!stopped) { setMap(m); setNoMap(false); } })
      .catch(() => { if (!stopped) setNoMap(true); });
    return () => { stopped = true; };
  }, [stamp, kind, camId, status && status.pending, status && status.can_revert]);

  useEffect(() => {
    if (status && status.maps && !status.maps[kind]) {
      const other = kind === "day" ? "night" : "day";
      if (status.maps[other]) setKind(other);
    }
  }, [status && status.maps && Object.keys(status.maps).join()]);

  async function run(path, body, done, base = BASE) {
    setBusy(true);
    const result = await act(post(`${base}/${path}`, base === BASE ? { camera: camId, ...body } : body), done);
    setBusy(false);
    setNote(result ? done : "That did not work. See the message above.");
    if (result) setTick((n) => n + 1);
    return result;
  }

  if (down) {
    return html`<div class="stack"><p class="alert tone-warn" role="alert">Assist is not answering.</p>
      <div><button type="button" onClick=${() => { setDown(false); setTick((n) => n + 1); }}>Try again</button></div></div>`;
  }
  if (!status) return html`<p class="boot" role="status">Loading Survey…</p>`;

  const run_ = status.run;
  const proposals = map ? map.proposals.filter((p) => p.type !== "frigate") : [];
  const pendingList = proposals.filter((p) => p.status === "pending");
  const geo = map ? map.geometry : null;
  const route = proposals.filter((p) => p.type === "route").slice(-1)[0];
  const presetPins = proposals.filter((p) => (p.type === "preset" && p.action === "create") || p.type === "night");
  const changeBlocks = map && map.changes ? map.changes.filter((c) => c.change !== "none") : [];
  const modeNow = status.mode;
  const mapNow = status.maps && status.maps[kind];

  function choose(item) {
    return async (what) => {
      if (what === "accept") await run(`proposals/${item.id}/accept`, {}, `${item.name} saved.`);
      else if (what === "reject") await run(`proposals/${item.id}/reject`, {}, "Rejected.");
      else setOpen(null);
      if (what === "accept" || what === "reject") setOpen(null);
    };
  }

  const picked = open ? proposals.find((p) => p.id === open) : null;
  const stops = route && route.stops ? route.stops : [];
  const byName = Object.fromEntries(stops.map((s) => [s.name, s]));
  const path = route ? route.points.map((p) => byName[p.name]).filter(Boolean) : [];

  return html`<div class="stack">
    <section aria-labelledby="survey-title">
      <div class="card-head">
        <h2 id="survey-title">Survey</h2>
        <div class="row">
          <button type="button" class="primary" disabled=${busy || running || modeNow === "off"}
            onClick=${() => run("start", {}, "Survey started.")}>${status.first_run ? "Run your first survey" : "Start survey"}</button>
          ${running ? html`<button type="button" disabled=${busy} onClick=${() => run("cancel", {}, "Stopping the survey.")}>Cancel</button>` : null}
          ${run_ && run_.status === "interrupted" ? html`<button type="button" disabled=${busy} onClick=${() => run("resume", {}, "Resuming.")}>Resume</button>` : null}
        </div>
      </div>
      ${status.first_run ? html`<p class="alert tone-ok" role="note">This camera has no presets and no patrol yet. A survey will sweep its whole range, map what it sees and propose both. It takes a few minutes and gives way to people and to Frigate.</p>` : null}
      ${modeNow === "off" ? html`<p class="alert tone-warn" role="note">Survey is off. Choose a level below to use it.</p>` : null}
      <${Progress} run=${run_} />
      <p class="assist-note" role="status" aria-live="polite">${note}</p>
      <div class="assist-seg" role="radiogroup" aria-label="Survey autonomy">
        ${MODES.map(([value, text]) => html`<button type="button" role="radio" key=${value} aria-checked=${modeNow === value} disabled=${busy}
          onClick=${() => run("settings", { capabilities: { survey: { mode: value } } }, `Survey set to ${value}.`, "api/v1/assist")}>${text}</button>`)}
      </div>
      <p class="muted">${modeNow === "auto" ? "Auto saves presets and rewrites the patrol after a survey, and you can revert it." : modeNow === "act" ? "Act also applies dwell weights from the map for a few hours at a time." : modeNow === "observe" ? "Observe builds the map and keeps proposals read only." : modeNow === "suggest" ? "Suggest keeps every change waiting for you." : ""}
        ${status.schedule ? ` Re-surveys weekly: day ${status.schedule.day || "never"}, night ${status.schedule.night || "never"}.` : " Scheduled re-surveys are off."}</p>
    </section>

    ${noMap || !map ? html`<${Card} id="survey-nomap" title="Panorama"><p class="muted">No ${kind} map yet. ${kind === "night" ? "Start a survey after dark to make one." : ""}</p>
      ${status.maps && Object.keys(status.maps).length ? html`<button type="button" onClick=${() => setKind(kind === "day" ? "night" : "day")}>Show the ${kind === "day" ? "night" : "day"} map</button>` : null}</${Card}>`
    : html`<${Card} id="survey-map" title=${`${kind === "night" ? "Night" : "Day"} panorama`}
      actions=${html`<div class="row">
        <div class="assist-seg" role="radiogroup" aria-label="Map">
          ${["day", "night"].map((k) => html`<button type="button" role="radio" key=${k} aria-checked=${kind === k}
            disabled=${!(status.maps && status.maps[k])} onClick=${() => { setKind(k); setOpen(null); }}>${k === "day" ? "Day" : "Night"}</button>`)}
        </div></div>`}>
      <div class="row survey-toggles">
        ${[["labels", "Regions"], ["pins", "Presets"], ["path", "Patrol path"], ["changes", `Changes (${changeBlocks.length})`]].map(([k, t]) => html`
          <label class="inline" key=${k}><input type="checkbox" checked=${view[k]} onChange=${(e) => setView({ ...view, [k]: e.currentTarget.checked })} /> ${t}</label>`)}
      </div>
      <div class="survey-stage" style=${`aspect-ratio:${map.size[0]} / ${map.size[1]}`}>
        <img class="survey-pano" src=${asset("panorama.jpg", camId, kind, map.id)} alt=${`Panorama of everything the camera can see, ${kind}`} />
        ${view.labels ? html`<img class="survey-overlay" src=${asset("overlay.png", camId, kind, map.id)} alt="" aria-hidden="true" />` : null}
        ${view.labels ? map.regions.filter((r) => r.area >= 300 || r.interest >= 0.5).map((r) => html`
          <span class="survey-label" key=${r.id} style=${{ ...place(map.extent, r.center[0], r.center[1]), borderColor: COLORS[r.label] }}>${LABEL[r.label] || r.label}</span>`) : null}
        ${view.path && path.length > 1 ? html`<svg class="survey-path" viewBox=${`0 0 ${map.extent[2] - map.extent[0]} ${map.extent[3] - map.extent[1]}`} preserveAspectRatio="none" aria-hidden="true">
          <polyline points=${path.map((s) => `${s.az - map.extent[0]},${map.extent[3] - s.el}`).join(" ")} />
        </svg>` : null}
        ${view.path ? path.map((s, i) => html`<span class="survey-step" key=${s.name} style=${place(map.extent, s.az, s.el)} aria-hidden="true">${i + 1}</span>`) : null}
        ${view.changes ? changeBlocks.map((c, i) => html`<span class="survey-change" key=${i} style=${place(map.extent, c.az, c.el)} title=${`${c.change.replace("_", " ")} (${Math.round(c.score * 100)}%)`}>!</span>`) : null}
        ${view.pins ? presetPins.map((p) => { const [a, e] = azel(geo, p); return html`
          <button type="button" key=${p.id} class=${`survey-pin tone-${PROP_TONE[p.status] || "muted"}${open === p.id ? " on" : ""}`}
            style=${place(map.extent, a, e)} aria-label=${`${p.name}, ${p.status}`} aria-pressed=${open === p.id}
            onClick=${() => setOpen(open === p.id ? null : p.id)}>${p.type === "night" ? "☾" : "●"}</button>`; }) : null}
      </div>
      <p class="muted">${when(mapNow.ts)}. ${map.regions.length} regions, ${Math.round(map.coverage * 100)}% covered${map.seams && map.seams.mean !== null ? `, seams ${Math.round(map.seams.mean * 100)}% aligned` : ""}${map.used_model ? "" : ". Labelled by rules only: add a model for better labels."}</p>
      ${picked ? html`<${Detail} item=${picked} geo=${geo} map=${map} camId=${camId} busy=${busy} run=${running} onClose=${choose(picked)} />` : null}
    </${Card}>`}

    ${map ? html`<${Card} id="survey-proposals" title=${`Proposals (${pendingList.length} waiting)`}
      actions=${html`<div class="row">
        ${pendingList.length && modeNow !== "observe" ? html`<button type="button" class="primary" disabled=${busy || running}
          onClick=${() => run("apply", {}, "Applied.")}>Apply all</button>` : null}
        ${status.can_revert ? html`<button type="button" disabled=${busy || running} onClick=${() => run("revert", {}, "Reverted.")}>Revert last apply</button>` : null}
        <a class="button" href=${pageUrl(`${BASE}/frigate.json${q(camId, `&kind=${kind}`)}`)} download="frigate-suggestions.json">Frigate suggestions</a>
      </div>`}>
      ${proposals.length ? html`<div class="table-wrap"><table>
        <thead><tr><th>What</th><th>Name</th><th>Why</th><th>Status</th><th><span class="sr-only">Actions</span></th></tr></thead>
        <tbody>${proposals.map((p) => html`<tr key=${p.id}>
          <td>${p.type === "preset" ? (p.action === "reuse" ? "Existing preset" : "New preset") : p.type === "night" ? "Night view" : "Patrol route"}</td>
          <td>${p.name || `${(p.points || []).length} stops`}</td><td>${p.reason || ""}</td>
          <td><${Badge} tone=${PROP_TONE[p.status] || "muted"}>${p.status}</${Badge}></td>
          <td><button type="button" aria-label=${`Details for ${p.name || p.type}`} onClick=${() => setOpen(p.id)}>Details</button></td></tr>`)}</tbody></table></div>`
        : html`<p class="muted">Nothing proposed. The map showed nothing that needs a preset.</p>`}
      ${changeBlocks.length ? html`<h3>Changed since the last ${kind} map</h3><ul>${changeBlocks.slice(0, 8).map((c, i) => html`<li key=${i}>${c.change.replace("_", " ")} near ${Math.round(c.az)}° across, ${Math.round(c.el)}° up</li>`)}</ul>` : null}
    </${Card}>` : null}
  </div>`;
}
