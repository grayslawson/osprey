// Side-effecting operations shared by the shell and the views: session,
// camera selection, the live event stream (with polling fallback), and
// REST mutations that report their outcome as toasts.
import { ApiError, cameraPath, del, get, pageUrl, post, setCsrf } from "./api.js";
import {
  bootError, cam, cameras, connection, logEntries, patchSnapshot, pushLogs, route,
  session, snapshot, toast,
} from "./store.js";

const POLL_MS = 5000;
const RETRY_STREAM_EVERY = 6; // polls between attempts to re-open the event stream

let source = null;
let pollTimer = 0;
let failures = 0;
let polls = 0;

export function errorText(error) {
  if (error instanceof ApiError) {
    return error.hint ? `${error.message} ${error.hint}` : error.message;
  }
  return "Something went wrong. Try again.";
}

// Run a mutation, toast the outcome, and return the response (or null).
export async function act(promise, success) {
  try {
    const result = await promise;
    if (success) toast(typeof success === "function" ? success(result) : success, "ok");
    return result;
  } catch (error) {
    toast(errorText(error), "bad");
    return null;
  }
}

export async function refreshSession() {
  const value = await get("api/v1/session");
  setCsrf(value.csrf);
  session.value = value;
  return value;
}

export function signedIn() {
  const value = session.value;
  return !!value && value.mode !== "locked" && (value.authenticated || value.mode === "disabled");
}

export async function loadCameras() {
  const data = await get("api/v1/cameras");
  cameras.value = data.cameras;
  return data.cameras;
}

export async function refreshState() {
  const id = cam.value;
  const state = await get(cameraPath(id, "/state"));
  if (cam.value === id) snapshot.value = state;
  return state;
}

export async function loadLogs() {
  try {
    const data = await get("api/v1/logs?limit=200&level=INFO");
    logEntries.value = data.entries.map((entry) => ({ ...entry }));
  } catch (error) {
    // Logs are advisory; the rest of the page still works without them.
  }
}

function on(target, name, handler) {
  target.addEventListener(name, (event) => {
    try {
      handler(JSON.parse(event.data));
    } catch (error) {
      // A malformed frame must not break the stream for everything else.
    }
  });
}

export function stopStreams() {
  if (source) source.close();
  source = null;
  clearInterval(pollTimer);
  pollTimer = 0;
}

export function startStreams(id) {
  stopStreams();
  failures = 0;
  polls = 0;
  connection.value = "connecting";
  const events = new EventSource(pageUrl(`api/v1/events?camera=${encodeURIComponent(id)}`));
  source = events;
  on(events, "state", (data) => {
    if (cam.value !== id) return;
    snapshot.value = data;
    failures = 0;
    connection.value = "live";
  });
  on(events, "ptz", (data) => patchSnapshot("ptz", data));
  on(events, "streams", (data) => patchSnapshot("streams", data));
  on(events, "health", (data) => patchSnapshot("health", data));
  on(events, "preset", (data) => patchSnapshot("presets", data.presets));
  on(events, "adoption", (data) => {
    const current = snapshot.value ? snapshot.value.camera || {} : {};
    patchSnapshot("camera", { ...current, ...data.camera, adopted: data.adopted });
  });
  on(events, "log", (entry) => pushLogs([entry]));
  on(events, "session", (data) => {
    if (!data.authenticated) {
      stopStreams();
      refreshSession().catch(() => {});
    }
  });
  events.onerror = () => {
    failures += 1;
    if (events.readyState === EventSource.CLOSED || failures >= 3) {
      events.close();
      if (source === events) startPolling(id);
    } else {
      connection.value = "connecting";
    }
  };
}

function startPolling(id) {
  connection.value = "polling";
  clearInterval(pollTimer);
  pollTimer = setInterval(() => pollOnce(id), POLL_MS);
}

async function pollOnce(id) {
  if (cam.value !== id) return;
  try {
    await refreshState();
    polls += 1;
    if (polls % RETRY_STREAM_EVERY === 0) startStreams(id);
  } catch (error) {
    if (error instanceof ApiError && (error.status === 401 || error.status === 503)) {
      stopStreams();
      await refreshSession().catch(() => {});
    } else {
      connection.value = "offline";
    }
  }
}

export function selectCamera(id) {
  if (!id || (id === cam.value && (source || pollTimer))) return;
  cam.value = id;
  snapshot.value = null;
  startStreams(id);
}

export async function boot() {
  route.value = parseHash();
  window.addEventListener("hashchange", () => {
    const next = parseHash();
    route.value = next;
    if (next.cam && next.cam !== cam.value && cameras.value.some((c) => c.id === next.cam)) {
      selectCamera(next.cam);
    }
  });
  try {
    await refreshSession();
    bootError.value = "";
  } catch (error) {
    bootError.value = errorText(error);
    return;
  }
  if (signedIn()) await enter();
}

// Called after a successful sign-in or when the session is already valid.
export async function enter() {
  try {
    const list = await loadCameras();
    const requested = route.value.cam;
    const first = list.length ? list[0].id : "default";
    const id = list.some((c) => c.id === requested) ? requested : first;
    cam.value = id;
    selectCamera(id);
    loadLogs();
  } catch (error) {
    bootError.value = errorText(error);
  }
}

export async function signIn(password) {
  const data = await post("api/v1/session", { password });
  setCsrf(data.csrf);
  session.value = { ...session.value, ...data };
  await enter();
}

export async function signOut() {
  stopStreams();
  await act(del("api/v1/session"), "Signed out");
  setCsrf("");
  await refreshSession().catch(() => {});
}

function parseHash() {
  return routeFromHash(window.location.hash);
}

export function routeFromHash(hash) {
  const [path, query = ""] = hash.replace(/^#/, "").split("?", 2);
  const name = path.replace(/^\//, "");
  const view = ["live", "presets", "health", "logs", "settings"].includes(name) ? name : "live";
  return { view, cam: new URLSearchParams(query).get("cam") || "" };
}

