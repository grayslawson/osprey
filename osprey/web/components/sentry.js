// The operator-facing patrol panel (Sentry). It reads one camera's patrol
// from /api/v1/cameras/{cam}/sentry, shows the live state, and lets the
// operator build the tour the camera sweeps through while it is idle.
import { useEffect, useRef, useState } from "preact/hooks";
import { html } from "./html.js";
import { Badge, Card } from "./common.js";
import { act, errorText } from "../actions.js";
import { ApiError, cameraPath, del, get, post } from "../api.js";
import { cam, sentry, snapshot, toast } from "../store.js";

const POLL_MS = 5000; // the app's poll cadence, the same as actions.js
const DEFAULT_MAX_SPEED = 1000; // G5 ceiling when the state document omits it
const DEFAULT_INTERVAL_SEC = 45;
const DEFAULT_TRACKING_HOLD_SEC = 15;

const STATE_TONE = {
  disabled: "muted",
  waiting: "ok",
  moving: "warn",
  tracking: "warn",
  camera_busy: "warn",
  busy: "warn",
  error: "bad",
};

const STATE_LABEL = {
  disabled: "Not running",
  waiting: "Waiting for the next move",
  moving: "Moving",
  tracking: "Standing down (autotracking)",
  camera_busy: "Camera busy",
  busy: "Camera in use",
  error: "Error",
};

function text(value, fallback) {
  return value === null || value === undefined ? String(fallback) : String(value);
}

// The G5 speed ceiling. The state document carries it as ptz.max_speed when
// the backend publishes it; otherwise the documented default is used.
function maxSpeed() {
  const state = snapshot.value;
  const value = state && state.ptz ? state.ptz.max_speed : null;
  return typeof value === "number" && value > 0 ? value : DEFAULT_MAX_SPEED;
}

// The camera's preset names. The patrol payload lists them; the state
// document is the fallback before a patrol exists.
function presetNames(body) {
  if (body && body.presets && body.presets.length) return body.presets;
  const state = snapshot.value;
  const list = state && state.presets ? state.presets : [];
  return list.map((preset) => preset.name);
}

function draftFrom(config) {
  const source = config || {};
  return {
    interval_sec: text(source.interval_sec, DEFAULT_INTERVAL_SEC),
    speed: text(source.speed, maxSpeed()),
    tracking_hold_sec: text(source.tracking_hold_sec, DEFAULT_TRACKING_HOLD_SEC),
    points: (source.points || []).map((point) => ({
      name: point.name,
      dwell_sec: point.dwell_sec === null || point.dwell_sec === undefined ? "" : String(point.dwell_sec),
    })),
  };
}

// Turn the draft into a POST body, or report the first problem with it.
function buildPayload(draft, enabled) {
  const points = [];
  for (let index = 0; index < draft.points.length; index += 1) {
    const point = draft.points[index];
    const name = point.name.trim();
    if (!name) return { error: `Patrol position ${index + 1} needs a name.` };
    const entry = { name };
    if (point.dwell_sec !== "") {
      const dwell = Number(point.dwell_sec);
      if (!Number.isFinite(dwell) || dwell < 1) {
        return { error: `Patrol position ${index + 1} needs a dwell of at least 1 second.` };
      }
      entry.dwell_sec = dwell;
    }
    points.push(entry);
  }
  const names = points.map((point) => point.name.toLowerCase());
  if (new Set(names).size !== names.length) {
    return { error: "Patrol positions must have different names." };
  }
  if (enabled === true && !points.length) {
    return { error: "Add at least one patrol position before starting the patrol." };
  }
  const interval = Number(draft.interval_sec);
  if (!Number.isFinite(interval) || interval < 1) {
    return { error: "The interval must be at least 1 second." };
  }
  const hold = Number(draft.tracking_hold_sec);
  if (!Number.isFinite(hold) || hold < 1) {
    return { error: "The tracking hold must be at least 1 second." };
  }
  const ceiling = maxSpeed();
  const speed = Number(draft.speed);
  if (!Number.isInteger(speed) || speed < 1 || speed > ceiling) {
    return { error: `The speed must be a whole number from 1 to ${ceiling}.` };
  }
  const payload = { interval_sec: interval, speed, tracking_hold_sec: hold, points };
  if (enabled !== undefined) payload.enabled = enabled;
  return { payload };
}

export function SentryPage() {
  const body = sentry.value;
  const [draft, setDraft] = useState(null);
  const [load, setLoad] = useState("loading"); // loading | ready | absent | unsupported | nocamera
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const dirty = useRef(false);

  useEffect(() => {
    const id = cam.value;
    let stopped = false;
    dirty.current = false;
    setDraft(null);
    setError("");
    setLoad("loading");
    sentry.value = null;

    async function poll() {
      try {
        const data = await get(cameraPath(id, "/sentry"));
        if (stopped || cam.value !== id) return;
        sentry.value = data;
        setLoad("ready");
        if (!dirty.current) setDraft(draftFrom(data.config));
      } catch (err) {
        if (stopped || cam.value !== id) return;
        if (err instanceof ApiError && err.code === "no_sentry") {
          // 404 means this camera has no patrol; 503 means the deployment has
          // no patrol at all. Neither is an operator error.
          setLoad(err.status === 503 ? "unsupported" : "absent");
          if (err.status !== 503 && !dirty.current) setDraft(draftFrom(null));
          return;
        }
        if (err instanceof ApiError && err.code === "unknown_camera") {
          setLoad("nocamera"); // keeps polling, so the page fills in once the camera adopts
          return;
        }
        setLoad("error");
        toast(errorText(err), "bad");
      }
    }

    poll();
    const timer = setInterval(poll, POLL_MS);
    return () => {
      stopped = true;
      clearInterval(timer);
    };
  }, [cam.value]);

  function update(patch) {
    dirty.current = true;
    setError("");
    setDraft((current) => ({ ...current, ...patch }));
  }

  function editPoints(change) {
    dirty.current = true;
    setError("");
    setDraft((current) => ({ ...current, points: change(current.points) }));
  }

  function addPoint() {
    const used = new Set((draft.points || []).map((point) => point.name.trim().toLowerCase()));
    const free = presetNames(body).find((name) => !used.has(name.toLowerCase()));
    editPoints((points) => [...points, { name: free || "", dwell_sec: "" }]);
  }

  function movePoint(index, delta) {
    editPoints((points) => {
      const target = index + delta;
      if (target < 0 || target >= points.length) return points;
      const next = [...points];
      const [item] = next.splice(index, 1);
      next.splice(target, 0, item);
      return next;
    });
  }

  async function apply(enabled) {
    const result = buildPayload(draft, enabled);
    if (result.error) {
      setError(result.error);
      return;
    }
    setBusy(true);
    const response = await act(
      post(cameraPath(cam.value, "/sentry"), result.payload),
      enabled === undefined ? "Patrol settings saved." : "Patrol started.",
    );
    setBusy(false);
    if (response) {
      dirty.current = false;
      sentry.value = response;
      setDraft(draftFrom(response.config));
      setError("");
    }
  }

  async function stop() {
    setBusy(true);
    const response = await act(post(cameraPath(cam.value, "/sentry"), { enabled: false }), "Patrol stopped.");
    setBusy(false);
    if (response) {
      dirty.current = false;
      sentry.value = response;
      setDraft(draftFrom(response.config));
      setError("");
    }
  }

  async function remove() {
    if (!window.confirm("Remove this camera's patrol? Its positions are deleted.")) return;
    setBusy(true);
    const response = await act(del(cameraPath(cam.value, "/sentry")), "Patrol removed.");
    setBusy(false);
    if (response) {
      dirty.current = false;
      sentry.value = null;
      setDraft(draftFrom(null));
      setLoad("absent");
      setError("");
    }
  }

  const state = body && body.state ? body.state : null;
  const config = body && body.config ? body.config : null;
  const movement = body && body.movement ? body.movement : null;
  const names = presetNames(body);
  const ceiling = maxSpeed();
  const tone = STATE_TONE[state ? state.state : "disabled"] || "muted";
  const label = STATE_LABEL[state ? state.state : "disabled"] || (state ? state.state : "unknown");
  const points = draft ? draft.points : [];

  return html`
    <div class="stack">
      <section aria-labelledby="patrol-title">
        <h2 id="patrol-title">Patrol</h2>
        <p class="muted">The idle patrol sweeps the camera through the positions below on a timer.
          It stands down while a person or Frigate is driving the camera.</p>
      </section>

      ${load === "unsupported"
        ? html`<p class="alert tone-warn" role="status">This deployment has no idle patrol.</p>`
        : null}

      ${load === "nocamera"
        ? html`<p class="alert tone-warn" role="status">No camera is connected yet. The patrol
          settings appear once the camera has adopted.</p>`
        : null}

      ${load === "absent"
        ? html`<p class="alert tone-warn" role="status">No patrol is configured for this camera.
          Add positions below and save to create one.</p>`
        : null}

      <${Card} id="patrol-status" title="Status"
        actions=${html`<${Badge} tone=${tone}>${label}</${Badge}>`}>
        <dl class="kv">
          <dt>Configured</dt><dd>${config ? "yes" : "no"}</dd>
          <dt>Running</dt><dd>${state && state.enabled ? "yes" : "no"}</dd>
          <dt>State</dt><dd>${label}</dd>
          <dt>Current position</dt><dd>${state && state.point ? state.point : "—"}</dd>
          <dt>Moves</dt><dd>${state ? state.moves : 0}</dd>
          <dt>Camera in use</dt>
          <dd>${movement && movement.busy ? `yes (${movement.owner || "someone"})` : "no"}</dd>
        </dl>
        ${state && state.last_error
          ? html`<p class="alert tone-bad" role="alert">${state.last_error}</p>`
          : null}
        ${state && state.points && state.points.length
          ? html`<p class="muted">Tour: ${state.points.join(" → ")}</p>`
          : null}
      </${Card}>

      ${load === "unsupported" || load === "nocamera" ? null : html`
        <${Card} id="patrol-settings" title="Patrol settings">
          ${!draft
            ? html`<p class="muted" role="status">Loading the patrol…</p>`
            : html`
              <form class="stack" onSubmit=${(event) => { event.preventDefault(); apply(undefined); }} noValidate>
                <div class="row">
                  <label>Interval (s)
                    <input type="number" name="interval" min="1" step="1" value=${draft.interval_sec}
                      onInput=${(event) => update({ interval_sec: event.currentTarget.value })} />
                  </label>
                  <label>Speed (1–${ceiling})
                    <input type="number" name="speed" min="1" max=${ceiling} step="1" value=${draft.speed}
                      onInput=${(event) => update({ speed: event.currentTarget.value })} />
                  </label>
                  <label>Tracking hold (s)
                    <input type="number" name="tracking-hold" min="1" step="1" value=${draft.tracking_hold_sec}
                      onInput=${(event) => update({ tracking_hold_sec: event.currentTarget.value })} />
                  </label>
                </div>
                <p class="muted">The interval is the time between moves. The speed is the G5 scale from 1 to ${ceiling}.
                  The tracking hold is how long the patrol stays out of the way after Frigate last moved the camera.</p>

                <section aria-labelledby="patrol-points-title">
                  <div class="card-head">
                    <h3 id="patrol-points-title">Patrol positions</h3>
                    <button type="button" onClick=${addPoint}>Add position</button>
                  </div>
                  ${points.length === 0
                    ? html`<p class="muted">No positions yet. Add one to build the tour.</p>`
                    : html`<ol class="checks">
                        ${points.map((point, index) => {
                          const known = names.includes(point.name);
                          return html`<li key=${index}>
                            <div class="row">
                              <span class="muted mono">${index + 1}</span>
                              <select aria-label=${`Position for patrol position ${index + 1}`}
                                value=${known ? point.name : ""}
                                onChange=${(event) => {
                                  const value = event.currentTarget.value;
                                  if (value) {
                                    editPoints((list) => list.map((item, i) => (i === index ? { ...item, name: value } : item)));
                                  }
                                }}>
                                <option value="">Type a name…</option>
                                ${names.map((name) => html`<option value=${name} key=${name}>${name}</option>`)}
                              </select>
                              ${known ? null : html`
                                <input class="point-name" aria-label=${`Custom name for patrol position ${index + 1}`}
                                  value=${point.name} maxLength="64"
                                  onInput=${(event) => {
                                    const value = event.currentTarget.value;
                                    editPoints((list) => list.map((item, i) => (i === index ? { ...item, name: value } : item)));
                                  }} />`}
                              <label class="point-dwell">Dwell (s)
                                <input type="number" min="1" step="1" value=${point.dwell_sec}
                                  aria-label=${`Dwell in seconds for patrol position ${index + 1}`}
                                  onInput=${(event) => {
                                    const value = event.currentTarget.value;
                                    editPoints((list) => list.map((item, i) => (i === index ? { ...item, dwell_sec: value } : item)));
                                  }} />
                              </label>
                              <button type="button" class="icon" aria-label=${`Move patrol position ${index + 1} up`}
                                disabled=${index === 0} onClick=${() => movePoint(index, -1)}>↑</button>
                              <button type="button" class="icon" aria-label=${`Move patrol position ${index + 1} down`}
                                disabled=${index === points.length - 1} onClick=${() => movePoint(index, 1)}>↓</button>
                              <button type="button" class="danger"
                                aria-label=${`Remove patrol position ${index + 1}`}
                                onClick=${() => editPoints((list) => list.filter((_, i) => i !== index))}>Remove</button>
                            </div>
                          </li>`;
                        })}
                      </ol>`}
                  <p class="muted">A position names a saved preset. Leave the dwell empty to use the interval instead.
                    A preset that does not exist yet is reported as an error when the patrol reaches it.</p>
                </section>

                ${error ? html`<p class="field-error" role="alert">${error}</p>` : null}

                <div class="row">
                  <button class="primary" type="submit" disabled=${busy}>Save settings</button>
                  <button type="button" disabled=${busy} onClick=${() => apply(true)}>Start patrol</button>
                  <button type="button" disabled=${busy || !config} onClick=${stop}>Stop patrol</button>
                  <button type="button" class="danger" disabled=${busy || !config} onClick=${remove}>Remove patrol</button>
                </div>
              </form>`}
        </${Card}>`}
    </div>`;
}
