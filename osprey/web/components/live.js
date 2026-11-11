import { useEffect, useRef, useState } from "preact/hooks";
import { signal } from "@preact/signals";
import { html } from "./html.js";
import { Badge, Card, PTZ_LABEL, toneForPtz } from "./common.js";
import { act, errorText, refreshState } from "../actions.js";
import { ApiError, cameraPath, del, pageUrl, post, put } from "../api.js";
import { cam, cameras, snapshot, toast } from "../store.js";

const SNAPSHOT_MS = 5000;
const HOLD_MS = 200;
const ZOOM_MIN_GAP_MS = 250;
const HOLD_KEYS = new Set(["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "a", "d", "w", "s", "A", "D", "W", "S"]);
const step = signal(5); // percent of each axis span per button or key press

const clamp = (value, low, high) => Math.min(high, Math.max(low, value));
const fmt = (value) => (value >= 0 ? "+" : "") + value.toFixed(2);
const camPost = (rest, body = {}) => post(cameraPath(cam.value, rest), body);
const ptzOf = () => (snapshot.value ? snapshot.value.ptz : null);

// Continuous motion without a continuous verb: the camera gets one bounded
// step per request, and a held input re-issues it. A 409 means the previous
// step is still running, so it is skipped rather than reported.
export function createHold() {
  let timer = 0;
  let vector = null;
  let busy = false;
  let lastError = "";

  async function send() {
    if (!vector || busy) return;
    busy = true;
    const current = vector;
    try {
      await camPost("/ptz/move", {
        mode: "continuous", pan: current.pan, tilt: current.tilt, zoom: 0, speed: current.speed,
      });
    } catch (error) {
      if (!(error instanceof ApiError && error.status === 409)) {
        const message = errorText(error);
        if (message !== lastError) toast(message, "bad");
        lastError = message;
        stop();
      }
    } finally {
      busy = false;
    }
  }

  function start(next) {
    vector = next;
    if (!timer) {
      timer = setInterval(send, HOLD_MS);
      send();
    }
  }

  function stop() {
    if (!timer) return;
    clearInterval(timer);
    timer = 0;
    vector = null;
    camPost("/ptz/stop").catch(() => {});
  }

  return { start, stop, active: () => timer !== 0 };
}

function useHold() {
  const [hold] = useState(createHold);
  useEffect(() => () => hold.stop(), []);
  return hold;
}

// Map a direction (dx, dy in -1..1, y up) to a unit direction and a speed.
function unitVector(x, y) {
  const magnitude = Math.min(1, Math.hypot(x, y));
  if (magnitude < 0.12) return null;
  return { pan: x / Math.hypot(x, y), tilt: y / Math.hypot(x, y), speed: magnitude };
}

function Joystick({ label }) {
  const hold = useHold();
  const base = useRef(null);
  const knob = useRef(null);
  const radius = useRef(60);

  function place(event) {
    const rect = base.current.getBoundingClientRect();
    const cx = rect.left + rect.width / 2;
    const cy = rect.top + rect.height / 2;
    radius.current = rect.width / 2 - 26;
    const dx = event.clientX - cx;
    const dy = event.clientY - cy;
    const scale = Math.min(1, radius.current / Math.max(1, Math.hypot(dx, dy)));
    const kx = dx * scale;
    const ky = dy * scale;
    knob.current.style.transform = `translate(${kx}px, ${ky}px)`;
    return unitVector(kx / radius.current, -ky / radius.current);
  }

  function down(event) {
    base.current.setPointerCapture(event.pointerId);
    const vector = place(event);
    if (vector) hold.start(vector);
  }

  function move(event) {
    if (!base.current.hasPointerCapture(event.pointerId)) return;
    const vector = place(event);
    if (vector) hold.start(vector);
    else hold.stop();
  }

  function up(event) {
    if (base.current.hasPointerCapture(event.pointerId)) base.current.releasePointerCapture(event.pointerId);
    knob.current.style.transform = "translate(0px, 0px)";
    hold.stop();
  }

  return html`
    <div class="joystick" ref=${base} role="group" aria-label=${label}
      onPointerDown=${down} onPointerMove=${move} onPointerUp=${up} onPointerCancel=${up}>
      <div class="knob" ref=${knob}></div>
    </div>`;
}

function Stage({ name, ptz, onHelp, announce }) {
  const ref = useRef(null);
  const hold = useHold();
  const pointers = useRef(new Map());
  const gesture = useRef(null);
  const zoomAt = useRef(0);
  const [calibrated, setCalibrated] = useState(true);
  const [videoFailed, setVideoFailed] = useState(false);
  const [snapFailed, setSnapFailed] = useState(false);
  const [tick, setTick] = useState(0);
  const preview = snapshot.value && snapshot.value.media ? snapshot.value.media.preview : null;
  const usable = !!ptz && ptz.supported;

  useEffect(() => {
    if (!videoFailed) return undefined;
    const timer = setInterval(() => {
      setSnapFailed(false);
      setTick((t) => t + 1);
    }, SNAPSHOT_MS);
    return () => clearInterval(timer);
  }, [videoFailed]);

  useEffect(() => {
    const element = ref.current;
    if (!element) return undefined;
    // Non-passive so the wheel zooms the view instead of scrolling the page.
    const onWheel = (event) => {
      event.preventDefault();
      zoomPulse(event.deltaY < 0 ? 1 : -1);
    };
    element.addEventListener("wheel", onWheel, { passive: false });
    return () => element.removeEventListener("wheel", onWheel);
  }, []);

  function zoomPulse(direction) {
    const now = Date.now();
    if (now - zoomAt.current < ZOOM_MIN_GAP_MS) return;
    zoomAt.current = now;
    camPost("/ptz/move", { mode: "continuous", pan: 0, tilt: 0, zoom: direction, speed: 0.6 })
      .catch((error) => {
        if (!(error instanceof ApiError && error.status === 409)) toast(errorText(error), "bad");
      });
  }

  function normalised(event) {
    const rect = ref.current.getBoundingClientRect();
    return {
      x: ((event.clientX - rect.left) / rect.width) * 2 - 1,
      y: 1 - ((event.clientY - rect.top) / rect.height) * 2,
      w: rect.width,
      h: rect.height,
    };
  }

  function pointerDown(event) {
    if (!usable) return;
    ref.current.setPointerCapture(event.pointerId);
    pointers.current.set(event.pointerId, { x: event.clientX, y: event.clientY });
    if (pointers.current.size === 1) {
      gesture.current = { x: event.clientX, y: event.clientY, moved: false, pinch: 0 };
    } else if (pointers.current.size === 2) {
      const [a, b] = [...pointers.current.values()];
      gesture.current = { pinch: Math.hypot(a.x - b.x, a.y - b.y), moved: true };
      hold.stop();
    }
  }

  function pointerMove(event) {
    if (!pointers.current.has(event.pointerId)) return;
    pointers.current.set(event.pointerId, { x: event.clientX, y: event.clientY });
    const g = gesture.current;
    if (!g) return;
    if (pointers.current.size === 2) {
      const [a, b] = [...pointers.current.values()];
      const distance = Math.hypot(a.x - b.x, a.y - b.y);
      if (Math.abs(distance - g.pinch) > 12) {
        zoomPulse(distance > g.pinch ? 1 : -1);
        g.pinch = distance;
      }
      return;
    }
    const rect = ref.current.getBoundingClientRect();
    const dx = event.clientX - g.x;
    const dy = event.clientY - g.y;
    if (!g.moved && Math.hypot(dx, dy) < 8) return;
    g.moved = true;
    // Grab semantics: the picture follows the pointer, so the camera moves the other way.
    const vector = unitVector(-dx / (rect.width / 2), dy / (rect.height / 2));
    if (vector) hold.start(vector);
    else hold.stop();
  }

  function pointerUp(event) {
    if (!pointers.current.has(event.pointerId)) return;
    const g = gesture.current;
    const wasTap = !!g && !g.moved && pointers.current.size === 1;
    const at = normalised(event);
    pointers.current.delete(event.pointerId);
    if (ref.current.hasPointerCapture(event.pointerId)) ref.current.releasePointerCapture(event.pointerId);
    if (wasTap) centerOn(at);
    if (pointers.current.size === 0) {
      hold.stop();
      gesture.current = null;
    }
  }

  async function centerOn(at) {
    if (!calibrated) {
      toast("Click-to-center is not available for this camera model. Use drag, the joystick or the buttons.", "bad");
      return;
    }
    try {
      await camPost("/ptz/center", { x: clamp(at.x, -1, 1), y: clamp(at.y, -1, 1) });
      announce("Centering on the selected point.");
    } catch (error) {
      if (error instanceof ApiError && error.code === "not_calibrated") {
        setCalibrated(false);
        toast(errorText(error), "bad");
      } else if (error instanceof ApiError && error.status === 409) {
        toast("The camera is still moving. Wait for it to settle, then click again.", "bad");
      } else {
        toast(errorText(error), "bad");
      }
    }
  }

  function keyDown(event) {
    if (!usable) return;
    const amount = (event.shiftKey ? 3 : 1) * step.value / 100;
    const keys = {
      ArrowLeft: [-1, 0], a: [-1, 0], A: [-1, 0],
      ArrowRight: [1, 0], d: [1, 0], D: [1, 0],
      ArrowUp: [0, 1], w: [0, 1], W: [0, 1],
      ArrowDown: [0, -1], s: [0, -1], S: [0, -1],
    };
    if (keys[event.key]) {
      event.preventDefault();
      if (event.repeat) return;
      const [x, y] = keys[event.key];
      hold.start({ pan: x, tilt: y, speed: event.shiftKey ? 1 : 0.5 });
      return;
    }
    if (event.key === "+" || event.key === "=") {
      event.preventDefault();
      camPost("/ptz/move", { mode: "relative", pan: 0, tilt: 0, zoom: amount }).catch((e) => toast(errorText(e), "bad"));
    } else if (event.key === "-" || event.key === "_") {
      event.preventDefault();
      camPost("/ptz/move", { mode: "relative", pan: 0, tilt: 0, zoom: -amount }).catch((e) => toast(errorText(e), "bad"));
    } else if (event.key === "h" || event.key === "H") {
      event.preventDefault();
      act(camPost("/ptz/home"), "Moving to Home.");
    } else if (event.key === " ") {
      event.preventDefault();
      hold.stop();
      camPost("/ptz/stop").catch(() => {});
      announce("Stopped.");
    } else if (event.key === "?") {
      event.preventDefault();
      onHelp();
    } else if (/^[1-9]$/.test(event.key)) {
      event.preventDefault();
      const presets = snapshot.value ? snapshot.value.presets || [] : [];
      const preset = presets[Number(event.key) - 1];
      if (preset) act(camPost(`/presets/${preset.token}/goto`), `Going to ${preset.name}.`);
    }
  }

  function keyUp(event) {
    if (HOLD_KEYS.has(event.key)) hold.stop();
  }

  const position = ptz && ptz.position ? ptz.position : null;
  const video = !videoFailed && preview ? pageUrl(preview) : null;
  const snapshotUrl = pageUrl(cameraPath(cam.value, "/snapshot.jpg")) + `?t=${tick}`;

  return html`
    <div class="stage" role="application" tabindex="0" ref=${ref}
      aria-label=${`Camera view of ${name}`} aria-describedby="stage-help"
      onPointerDown=${pointerDown} onPointerMove=${pointerMove}
      onPointerUp=${pointerUp} onPointerCancel=${pointerUp}
      onKeyDown=${keyDown} onKeyUp=${keyUp} onBlur=${() => hold.stop()}>
      ${video
        ? html`<img class="video" src=${video} alt=${`Live video from ${name}`}
            onError=${() => setVideoFailed(true)} />`
        : snapFailed
          ? html`<div class="placeholder"><p>No picture yet. Live video appears once the camera streams.</p></div>`
          : html`<img class="snap" src=${snapshotUrl} alt=${`Latest snapshot from ${name}`}
              onError=${() => setSnapFailed(true)} />`}
      <span class="reticle" aria-hidden="true"></span>
      <div class="hud" aria-hidden="true">
        <span>${position ? `pan ${fmt(position.pan)} · tilt ${fmt(position.tilt)}` : "position unknown"}</span>
        <span>${position ? `zoom ${Math.round(position.zoom * 100)}%` : "zoom —"}</span>
        <span>${ptz && ptz.supported ? PTZ_LABEL[ptz.status] || ptz.status : "no PTZ"}</span>
      </div>
    </div>
    <p id="stage-help" class="muted">
      Drag or click the view, or use the arrow keys (W A S D also work). Plus and minus zoom,
      H goes Home, Space stops, 1 to 9 go to presets, and question mark lists all keys.
    </p>`;
}

function Pad({ label }) {
  const move = (pan, tilt) => () => {
    camPost("/ptz/move", { mode: "relative", pan: pan * step.value / 100, tilt: tilt * step.value / 100, zoom: 0 })
      .catch((error) => toast(errorText(error), "bad"));
  };
  return html`
    <div class="pad" role="group" aria-label=${label}>
      <span class="center"></span>
      <button type="button" aria-label="Pan up" onClick=${move(0, 1)}>Up</button>
      <span class="center"></span>
      <button type="button" aria-label="Pan left" onClick=${move(-1, 0)}>Left</button>
      <button type="button" aria-label="Stop" class="danger" onClick=${() => camPost("/ptz/stop").catch(() => {})}>Stop</button>
      <button type="button" aria-label="Pan right" onClick=${move(1, 0)}>Right</button>
      <span class="center"></span>
      <button type="button" aria-label="Pan down" onClick=${move(0, -1)}>Down</button>
      <span class="center"></span>
    </div>`;
}

function ZoomControl() {
  const ptz = ptzOf();
  const current = ptz && ptz.position ? Math.round(ptz.position.zoom * 100) : 0;
  const [value, setValue] = useState(current);
  const timer = useRef(0);
  useEffect(() => setValue(current), [current]);

  function absolute(next) {
    clearTimeout(timer.current);
    timer.current = setTimeout(() => {
      camPost("/ptz/move", { mode: "absolute", zoom: next / 100 })
        .catch((error) => toast(errorText(error), "bad"));
    }, 350);
  }

  return html`
    <div class="row">
      <button type="button" aria-label="Zoom out" onClick=${() => camPost("/ptz/move", { mode: "relative", pan: 0, tilt: 0, zoom: -step.value / 100 }).catch((e) => toast(errorText(e), "bad"))}>−</button>
      <label class="stack">
        <span>Zoom <span class="mono">${value}%</span></span>
        <input type="range" min="0" max="100" step="1" value=${value} aria-valuetext=${`${value} percent`}
          onInput=${(event) => { const next = Number(event.currentTarget.value); setValue(next); absolute(next); }} />
      </label>
      <button type="button" aria-label="Zoom in" onClick=${() => camPost("/ptz/move", { mode: "relative", pan: 0, tilt: 0, zoom: step.value / 100 }).catch((e) => toast(errorText(e), "bad"))}>+</button>
    </div>`;
}

function Controls({ onHelp }) {
  return html`
    <div class="stack">
      <label>Step size <span class="mono">${step.value}%</span>
        <input type="range" min="1" max="20" step="1" value=${step.value} aria-valuetext=${`${step.value} percent`}
          onInput=${(event) => { step.value = Number(event.currentTarget.value); }} />
      </label>
      <${Pad} label="Pan and tilt" />
      <div class="row">
        <span class="muted">Joystick</span>
      </div>
      <${Joystick} label="Joystick: press and drag, release to stop" />
      <${ZoomControl} />
      <div class="row">
        <button type="button" onClick=${() => act(camPost("/ptz/home"), "Moving to Home.")}>Go to Home</button>
        <button type="button" onClick=${() => act(camPost("/ptz/home/save"), "Home saved to the current view.")}>Save current view as Home</button>
        <button type="button" onClick=${onHelp}>Keyboard help</button>
      </div>
    </div>`;
}

function PtzHealth({ ptz }) {
  if (!ptz || !ptz.supported) {
    return html`<${Card} id="ptz-health" title="PTZ">
      <p>This camera has no PTZ. Live video and snapshots still work.</p></${Card}>`;
  }
  const unknown = ptz.status === "UNKNOWN";
  return html`
    <${Card} id="ptz-health" title="PTZ health"
      actions=${html`<${Badge} tone=${toneForPtz(ptz.status)}>${PTZ_LABEL[ptz.status] || ptz.status}</${Badge}>`}>
      ${unknown
        ? html`<div class="alert tone-bad" role="alert">
            <p><strong>PTZ state unknown.</strong> ${ptz.error || "The motor did not report completion."}</p>
            <p class="muted">Presets and Home cannot be saved until the state is recovered.</p>
          </div>`
        : html`<p class="muted">The PTZ channel reports ${(PTZ_LABEL[ptz.status] || ptz.status).toLowerCase()}.</p>`}
      <div class="row">
        <button type="button" class=${unknown ? "primary" : ""}
          onClick=${() => act(camPost("/ptz/recover"), (r) => r.message || "Recovery requested.")}>Recover PTZ</button>
      </div>
    </${Card}>`;
}

function Streams({ streams }) {
  const tracks = Object.entries(streams || {});
  return html`
    <${Card} id="streams-title" title="Streams">
      ${tracks.length === 0
        ? html`<p class="muted">No streams reported yet.</p>`
        : html`<div class="chips">${tracks.map(([name, stat]) => html`
            <span class="pill tone-${stat.playable ? "ok" : "warn"}" key=${name}>
              <span class="dot" aria-hidden="true"></span>${name}: ${stat.playable ? "playable" : "waiting for a keyframe"}
              <span class="muted">· ${stat.subscribers} viewer${stat.subscribers === 1 ? "" : "s"}</span>
            </span>`)}</div>`}
    </${Card}>`;
}

function PresetsPanel({ presets, ptz }) {
  const list = presets || [];
  const [name, setName] = useState("");
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const ptzAvailable = !!ptz && !!ptz.supported && ptz.status !== "UNKNOWN";

  async function save(event) {
    event.preventDefault();
    const trimmed = name.trim();
    if (!trimmed) {
      setError("Enter a name for this position.");
      return;
    }
    setError("");
    setSaving(true);
    const result = await act(
      camPost("/presets", { name: trimmed }),
      (r) => `Saved ${r.preset ? r.preset.name : trimmed}.`,
    );
    setSaving(false);
    if (result) {
      setName("");
      await refreshState().catch(() => {});
    }
  }

  async function update(preset) {
    const result = await act(
      put(cameraPath(cam.value, `/presets/${preset.token}`)),
      `Updated ${preset.name} to the current view.`,
    );
    if (result) await refreshState().catch(() => {});
  }

  async function remove(preset) {
    if (!window.confirm(`Delete the preset "${preset.name}"? This cannot be undone.`)) return;
    const result = await act(
      del(cameraPath(cam.value, `/presets/${preset.token}`)),
      `Deleted ${preset.name}.`,
    );
    if (result) await refreshState().catch(() => {});
  }

  if (!ptz || !ptz.supported) return null;

  return html`
    <${Card} id="live-presets" title="Presets">
      <div class="stack">
        <form class="row" onSubmit=${save} noValidate>
          <input name="preset-name" maxLength="64" value=${name} required
            placeholder="Preset name (e.g. Driveway)"
            aria-label="Preset name"
            aria-describedby="preset-error" aria-invalid=${error ? "true" : "false"}
            onInput=${(event) => setName(event.currentTarget.value)} />
          <button class="primary" type="submit" disabled=${!ptzAvailable || saving}>
            ${saving ? "Saving…" : "Save current view"}</button>
        </form>
        ${error ? html`<p id="preset-error" class="field-error" role="alert">${error}</p>` : null}
        <p class="muted">Move the camera in the live view above, then save a new preset or update an existing one. Keys 1–9 jump to the first nine presets.</p>
        ${list.length === 0
          ? html`<p class="muted">No positions saved yet.</p>`
          : html`<ul class="preset-grid" role="list">
              ${list.map((preset, index) => html`
                <li class="preset" key=${preset.token}>
                  <div class="thumb">
                    ${preset.thumbnail
                      ? html`<img alt="" src=${pageUrl(cameraPath(cam.value, `/presets/${preset.token}/thumbnail.jpg`))} />`
                      : html`<span>No thumbnail</span>`}
                  </div>
                  <div class="body">
                    <div class="row">
                      ${index < 9 ? html`<span class="pill mono">#${index + 1}</span>` : null}
                      <h3>${preset.name}</h3>
                    </div>
                    ${preset.home ? html`<span class="pill tone-ok">Home · Frigate return target</span>` : null}
                  </div>
                  <div class="actions">
                    <button type="button" class="primary"
                      aria-label=${`Go to preset ${preset.name}${index < 9 ? ` (key ${index + 1})` : ""}`}
                      onClick=${() => act(camPost(`/presets/${preset.token}/goto`), `Going to ${preset.name}.`)}
                      disabled=${!ptz.supported}>Go to</button>
                    <button type="button" onClick=${() => update(preset)} disabled=${!ptzAvailable}>Update to current view</button>
                    <button type="button" class="danger" onClick=${() => remove(preset)} disabled=${!ptz.supported}>Delete</button>
                  </div>
                </li>`)}
            </ul>`}
      </div>
    </${Card}>`;
}

export function LivePage() {
  const state = snapshot.value;
  const [helpOpen, setHelpOpen] = useState(false);
  const [note, setNote] = useState("");
  const dialog = useRef(null);
  const current = cameras.value.find((c) => c.id === cam.value);
  const name = state && state.camera ? state.camera.name || cam.value : current ? current.name : cam.value;

  useEffect(() => {
    const element = dialog.current;
    if (!element) return;
    if (helpOpen && !element.open) element.showModal();
    if (!helpOpen && element.open) element.close();
  }, [helpOpen]);

  if (!state) {
    return html`<p class="boot" role="status">Waiting for the first state from ${name}…</p>`;
  }

  const ptz = state.ptz;
  const camera = state.camera;
  const waiting = !camera || !camera.adopted;
  return html`
    <div class="live">
      <div class="stack">
        ${waiting ? html`<div class="alert tone-warn" role="status">
          Waiting for the camera to connect and be adopted. Live video starts after that.</div>` : null}
        <${Stage} name=${name} ptz=${ptz} onHelp=${() => setHelpOpen(true)} announce=${setNote} />
        <p class="sr-only" aria-live="polite">${note}</p>
        <${PresetsPanel} presets=${state.presets} ptz=${ptz} />
        <${Streams} streams=${state.streams} />
      </div>
      <aside class="stack" aria-label="Controls">
        <${PtzHealth} ptz=${ptz} />
        ${ptz && ptz.supported ? html`<${Card} id="controls-title" title="Controls"><${Controls} onHelp=${() => setHelpOpen(true)} /></${Card}>` : null}
      </aside>
      <dialog ref=${dialog} aria-labelledby="help-title" onClose=${() => setHelpOpen(false)}>
        <h2 id="help-title">Keyboard shortcuts</h2>
        <dl class="help-list">
          <dt>Arrow keys or W A S D</dt><dd>Pan and tilt while held. Hold Shift for faster movement.</dd>
          <dt>+ and −</dt><dd>Zoom in and out.</dd>
          <dt>H</dt><dd>Go to Home.</dd>
          <dt>Space</dt><dd>Stop all movement.</dd>
          <dt>1 to 9</dt><dd>Go to the matching preset in the list.</dd>
          <dt>Click or tap</dt><dd>Center the view on that point (needs field-of-view calibration).</dd>
          <dt>Drag</dt><dd>Pan while held; release to stop.</dd>
          <dt>Mouse wheel or pinch</dt><dd>Zoom.</dd>
        </dl>
        <form method="dialog"><button type="submit" class="primary">Close</button></form>
      </dialog>
    </div>`;
}
