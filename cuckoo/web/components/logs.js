import { useEffect, useState } from "preact/hooks";
import { html } from "./html.js";
import { loadLogs } from "../actions.js";
import { logEntries } from "../store.js";

const LEVELS = { INFO: 20, WARNING: 30, ERROR: 40, CRITICAL: 50 };

function toneOf(level) {
  if (level === "ERROR" || level === "CRITICAL") return "bad";
  if (level === "WARNING") return "warn";
  return "muted";
}

function download(lines) {
  const blob = new Blob([lines.join("\n") + "\n"], { type: "text/plain" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = "osprey-log.txt";
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function LogsPage() {
  const [level, setLevel] = useState("INFO");
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState("");

  useEffect(() => {
    loadLogs();
  }, []);

  const needle = query.trim().toLowerCase();
  const visible = logEntries.value.filter((entry) =>
    (LEVELS[entry.level] || 0) >= LEVELS[level] &&
    (!needle || `${entry.logger} ${entry.message}`.toLowerCase().includes(needle)));
  const lines = visible.map((e) => `${e.timestamp} ${e.level} ${e.logger}: ${e.message}`);

  async function copy() {
    try {
      await navigator.clipboard.writeText(lines.join("\n"));
      setStatus(`Copied ${lines.length} records.`);
    } catch (error) {
      setStatus("Copy is not available here. Use Download instead.");
    }
  }

  return html`
    <section aria-labelledby="logs-title">
      <h2 id="logs-title">Logs</h2>
      <div class="log-toolbar">
        <label>Minimum level
          <select value=${level} onChange=${(event) => setLevel(event.currentTarget.value)}>
            ${["INFO", "WARNING", "ERROR"].map((name) => html`<option value=${name} key=${name}>${name}</option>`)}
          </select>
        </label>
        <label>Search
          <input type="search" value=${query} onInput=${(event) => setQuery(event.currentTarget.value)} />
        </label>
        <button type="button" onClick=${() => loadLogs()}>Refresh</button>
        <button type="button" onClick=${copy}>Copy</button>
        <button type="button" onClick=${() => download(lines)}>Download</button>
      </div>
      <p class="muted" role="status">${visible.length} record(s) shown. ${status}</p>
      <ol class="log-list" aria-label="Log records, newest first">
        ${visible.map((entry) => html`<li class="tone-${toneOf(entry.level)}" key=${entry.seq}>
          <span class="muted">${entry.timestamp}</span> <strong>${entry.level}</strong>
          <span class="muted">${entry.logger}</span> ${entry.message}</li>`)}
      </ol>
    </section>`;
}
