// Application state as signals. Components read these directly; the SSE
// client and the REST actions write them.
import { signal } from "@preact/signals";

export const session = signal(null); // {mode, authenticated, principal, csrf, ingress}
export const cameras = signal([]); // [{id, name, model, adopted, ptz, status}]
export const cam = signal("default"); // selected camera id
export const snapshot = signal(null); // full state document (see webapi.state)
export const connection = signal("connecting"); // connecting | live | polling | offline
export const bootError = signal("");
export const logEntries = signal([]); // newest first, capped
export const toasts = signal([]);
export const route = signal({ view: "live", cam: "" });

const LOG_CAP = 500;
let toastSeq = 0;

export function toast(message, tone = "ok") {
  const id = ++toastSeq;
  toasts.value = [...toasts.value, { id, message, tone }].slice(-4);
  setTimeout(() => {
    toasts.value = toasts.value.filter((t) => t.id !== id);
  }, tone === "bad" ? 8000 : 4000);
}

export function patchSnapshot(key, value) {
  if (snapshot.value) snapshot.value = { ...snapshot.value, [key]: value };
}

export function pushLogs(entries) {
  if (!entries.length) return;
  const seen = new Set(logEntries.value.map((e) => e.seq));
  // Live entries arrive oldest first; the list is newest first.
  const fresh = entries.filter((e) => !seen.has(e.seq)).sort((a, b) => b.seq - a.seq);
  if (!fresh.length) return;
  logEntries.value = [...fresh, ...logEntries.value].slice(0, LOG_CAP);
}

const THEME_KEY = "osprey.theme";
const THEMES = ["auto", "light", "dark"];

function readTheme() {
  try {
    const stored = window.localStorage.getItem(THEME_KEY);
    return THEMES.includes(stored) ? stored : "auto";
  } catch (error) {
    return "auto"; // storage can be blocked; the default still works
  }
}

export const theme = signal(readTheme());

export function cycleTheme() {
  const next = THEMES[(THEMES.indexOf(theme.value) + 1) % THEMES.length];
  theme.value = next;
  if (next === "auto") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", next);
  try {
    window.localStorage.setItem(THEME_KEY, next);
  } catch (error) {
    // Not persisted; the choice lasts for this page view.
  }
}

export function applyStoredTheme() {
  if (theme.value !== "auto") document.documentElement.setAttribute("data-theme", theme.value);
}
