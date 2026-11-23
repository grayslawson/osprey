// First-run wizard. Plain script (no framework) so the page loads before the
// operator app; every value that came from the network is set as text, never HTML.
"use strict";
(() => {
  const $ = (selector, root = document) => root.querySelector(selector);
  const sections = [...document.querySelectorAll("section[data-step]")];
  const state = { token: "", trusted: false, values: {}, boot: "" };

  // The token arrives in the link; keep it for this tab and drop it from the address bar.
  const params = new URLSearchParams(location.search);
  try {
    if (params.get("token")) sessionStorage.setItem("osprey-setup-token", params.get("token"));
    state.token = sessionStorage.getItem("osprey-setup-token") || "";
  } catch {
    state.token = params.get("token") || "";
  }
  if (params.has("token")) history.replaceState(null, "", location.pathname);

  const progress = $("#progress");
  for (const section of sections) {
    const item = document.createElement("li");
    item.textContent = section.dataset.title;
    item.dataset.for = section.dataset.step;
    progress.append(item);
  }

  function show(step) {
    error("");
    let reached = false;
    for (const section of sections) section.hidden = section.dataset.step !== step;
    for (const item of progress.children) {
      const current = item.dataset.for === step;
      if (current) reached = true;
      item.classList.toggle("done", !reached);
      if (current) item.setAttribute("aria-current", "step"); else item.removeAttribute("aria-current");
    }
    const focus = $(`section[data-step="${step}"] input, section[data-step="${step}"] button:not([disabled])`);
    if (focus) focus.focus();
  }

  function next(from) {
    const index = sections.findIndex((s) => s.dataset.step === from);
    show(sections[index + 1].dataset.step);
  }

  function back(from) {
    const index = sections.findIndex((s) => s.dataset.step === from);
    show(sections[Math.max(0, index - 1)].dataset.step);
  }

  function error(message) {
    const box = $("#error");
    box.textContent = message;
    box.hidden = !message;
  }

  async function call(method, path, body, headers = {}) {
    const init = { method, headers: { ...headers }, cache: "no-store", credentials: "same-origin" };
    if (state.token) init.headers["X-Setup-Token"] = state.token;
    if (body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body);
    }
    const response = await fetch(path, init);
    let data = {};
    try { data = await response.json(); } catch { /* empty or non-JSON body */ }
    if (!response.ok) {
      const detail = typeof data.error === "string" ? data.error : data.error && data.error.message;
      const failure = new Error(detail || `request failed (${response.status})`);
      failure.status = response.status;
      failure.data = data;
      throw failure;
    }
    return data;
  }

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  async function bootId() {
    try { return (await call("GET", "api/v1/session")).boot || ""; } catch { return ""; }
  }

  // Wait until a different Osprey process answers: the save restarts it.
  async function waitForRestart(before, seconds = 60) {
    const deadline = Date.now() + seconds * 1000;
    while (Date.now() < deadline) {
      await sleep(1000);
      const now = await bootId();
      if (now && now !== before) return true;
    }
    return false;
  }

  for (const button of document.querySelectorAll("[data-next]")) {
    button.addEventListener("click", () => next(button.closest("section").dataset.step));
  }
  for (const button of document.querySelectorAll("[data-back]")) {
    button.addEventListener("click", () => back(button.closest("section").dataset.step));
  }

  function collect(form) {
    Object.assign(state.values, Object.fromEntries(new FormData(form)));
  }

  $("#network-form").addEventListener("submit", (event) => {
    event.preventDefault();
    collect(event.target);
    next("network");
  });

  $("#camera-form").addEventListener("submit", (event) => {
    event.preventDefault();
    collect(event.target);
    next("camera");
  });

  $("#frigate-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    collect(event.target);
    const button = event.target.querySelector("button[type=submit]");
    button.disabled = true;
    try {
      await save();
    } catch (failure) {
      show("frigate");
      error(failure.message);
    } finally {
      button.disabled = false;
    }
  });

  async function save() {
    state.boot = await bootId();
    const result = await call("POST", "api/setup", state.values);
    show("saved");
    const credentials = result.generated_credentials || {};
    $("#credentials-text").textContent = Object.entries(credentials).map(([k, v]) => `${k}: ${v}`).join("\n");
    $("#credentials").hidden = !Object.keys(credentials).length;
    const status = $("#saved-status");
    if (!result.restarting) {
      status.textContent = "Saved. Restart Osprey to apply the configuration.";
      return;
    }
    status.textContent = "Saved. Osprey is restarting with the new configuration…";
    status.textContent = (await waitForRestart(state.boot))
      ? "Osprey restarted with the new configuration."
      : "Osprey has not come back yet. Check its log, then reload this page.";
  }

  async function start() {
    const status = $("#welcome-status");
    show("welcome");
    try {
      const info = await call("GET", "api/setup");
      state.trusted = Boolean(info.ingress);
      const host = $("#network-form [name=host]");
      host.value = info.host || "";
      $("#bridge-hint").hidden = !info.bridge_hint;
      status.textContent = "Ready.";
      $("section[data-step=welcome] [data-next]").disabled = false;
    } catch (failure) {
      status.textContent = failure.status === 404
        ? "Setup is already complete."
        : `${failure.message}. Open the setup link from the Osprey log.`;
    }
  }

  start();
})();
