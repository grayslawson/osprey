// Osprey operator app: a no-build ES-module shell (Preact + htm + signals,
// vendored under ui/vendor). Served as static files by cuckoo/webapi.py.
import "@preact/signals";
import { render } from "preact";
import { html } from "./components/html.js";
import { Loading, Toasts } from "./components/common.js";
import { Locked, Login } from "./components/session.js";
import { LivePage } from "./components/live.js";
import { PresetsPage } from "./components/presets.js";
import { HealthPage } from "./components/health.js";
import { LogsPage } from "./components/logs.js";
import { SettingsPage } from "./components/settings.js";
import { boot, selectCamera, signOut } from "./actions.js";
import {
  applyStoredTheme, bootError, cam, cameras, connection, cycleTheme, route, session, theme,
} from "./store.js";

const VIEWS = [
  ["live", "Live"],
  ["presets", "Presets"],
  ["health", "Health"],
  ["logs", "Logs"],
  ["settings", "Settings"],
];

function ThemeButton() {
  const label = theme.value;
  return html`<button type="button" onClick=${cycleTheme}>Theme: <span>${label}</span></button>`;
}

function CameraSwitch() {
  const list = cameras.value;
  if (list.length < 2) return null;
  return html`
    <label class="inline">Camera
      <select value=${cam.value} onChange=${(event) => {
        const id = event.currentTarget.value;
        window.location.hash = `#/${route.value.view}?cam=${encodeURIComponent(id)}`;
        selectCamera(id);
      }}>
        ${list.map((c) => html`<option value=${c.id} key=${c.id}>${c.name}</option>`)}
      </select>
    </label>`;
}

function ConnectionPill() {
  const labels = {
    live: ["Live", "ok"],
    polling: ["Polling every 5 s", "warn"],
    connecting: ["Connecting", "muted"],
    offline: ["Offline", "bad"],
  };
  const [text, tone] = labels[connection.value] || labels.connecting;
  return html`<span class="pill tone-${tone}" role="status">
    <span class="dot" aria-hidden="true"></span>${text}</span>`;
}

function Header() {
  const value = session.value;
  const canSignOut = !!value && value.mode === "password" && value.authenticated;
  return html`
    <header class="app-header">
      <div class="brand">Osprey <small>operator console</small></div>
      <${CameraSwitch} />
      <${ConnectionPill} />
      <${ThemeButton} />
      ${canSignOut ? html`<button type="button" onClick=${signOut}>Sign out</button>` : null}
    </header>`;
}

function Nav() {
  const active = route.value.view;
  const suffix = cam.value ? `?cam=${encodeURIComponent(cam.value)}` : "";
  return html`
    <nav class="tabs" aria-label="Sections">
      ${VIEWS.map(([key, label]) => html`
        <a href=${`#/${key}${suffix}`} key=${key} aria-current=${active === key ? "page" : null}>${label}</a>`)}
    </nav>`;
}

function View() {
  switch (route.value.view) {
    case "presets":
      return html`<${PresetsPage} />`;
    case "health":
      return html`<${HealthPage} />`;
    case "logs":
      return html`<${LogsPage} />`;
    case "settings":
      return html`<${SettingsPage} />`;
    default:
      return html`<${LivePage} />`;
  }
}

function Shell() {
  return html`
    <${Header} />
    <${Nav} />
    <main id="main" class="app-main" tabindex="-1">
      ${bootError.value ? html`<p class="alert tone-bad" role="alert">${bootError.value}</p>` : null}
      <${View} />
    </main>
    <${Toasts} />`;
}

function App() {
  const value = session.value;
  if (!value) {
    if (bootError.value) {
      return html`<main id="main" class="centered" tabindex="-1">
        <h1>Osprey cannot be reached</h1>
        <p class="alert tone-bad" role="alert">${bootError.value}</p>
        <button type="button" class="primary" onClick=${() => window.location.reload()}>Try again</button>
      </main>`;
    }
    return html`<${Loading} />`;
  }
  if (value.mode === "locked") return html`<${Locked} />`;
  if (value.mode === "password" && !value.authenticated) return html`<${Login} />`;
  return html`<${Shell} />`;
}

applyStoredTheme();
render(html`<${App} />`, document.getElementById("app"));
boot();
