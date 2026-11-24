// First-run wizard. Plain script (no framework) so the page loads before the
// operator app; every value that came from the network is set as text, never HTML.
"use strict";
(() => {
  const $ = (selector, root = document) => root.querySelector(selector);
  const sections = [...document.querySelectorAll("section[data-step]")];
  const state = { token: "", csrf: "", values: {}, boot: "", cameras: [], camera: null };
  const DOCS = "https://github.com/grayslawson/osprey/blob/main/docs/";

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
    if (!section.dataset.title) continue;
    const item = document.createElement("li");
    item.textContent = section.dataset.title;
    item.dataset.for = section.dataset.step;
    progress.append(item);
  }

  function show(step) {
    error("");
    for (const section of sections) section.hidden = section.dataset.step !== step;
    const order = sections.map((s) => s.dataset.step);
    for (const item of progress.children) {
      const current = item.dataset.for === step;
      item.classList.toggle("done", order.indexOf(item.dataset.for) < order.indexOf(step));
      if (current) item.setAttribute("aria-current", "step"); else item.removeAttribute("aria-current");
    }
    const focus = $(`section[data-step="${step}"] input, section[data-step="${step}"] button:not([disabled])`);
    if (focus) focus.focus();
  }

  function next(from) {
    const index = sections.findIndex((s) => s.dataset.step === from);
    let target = index + 1;
    while (sections[target] && !sections[target].dataset.title) target += 1;
    show(sections[target].dataset.step);
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

  function element(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  }

  async function call(method, path, body) {
    const init = { method, headers: {}, cache: "no-store", credentials: "same-origin" };
    if (state.token) init.headers["X-Setup-Token"] = state.token;
    if (state.csrf) init.headers["X-CSRF-Token"] = state.csrf;
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

  // After the restart the steps use the normal admin session, as the console does.
  async function ensureSession() {
    const session = await call("GET", "api/v1/session");
    if (session.authenticated) {
      state.csrf = session.csrf;
      return true;
    }
    if (state.values.admin_password) {
      try {
        state.csrf = (await call("POST", "api/v1/session", { password: state.values.admin_password })).csrf;
        return true;
      } catch { /* fall through to the sign-in form */ }
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
    if (!state.cameras.length) scan();
  });

  // ---------------------------------------------------------------- camera

  const cameraForm = $("#camera-form");

  function badge(text, kind) {
    return element("span", text, `badge ${kind}`);
  }

  function pick(camera) {
    state.camera = camera;
    cameraForm.elements.ip.value = camera.ip;
    cameraForm.elements.mac.value = camera.mac;
    if (camera.hostname) cameraForm.elements.name.value = camera.hostname.slice(0, 64);
    $("#managed-help").hidden = camera.managed !== true;
  }

  function renderCameras() {
    const list = $("#cameras");
    list.replaceChildren();
    state.cameras.forEach((camera, index) => {
      const item = document.createElement("li");
      const label = document.createElement("label");
      const radio = document.createElement("input");
      radio.type = "radio";
      radio.name = "pick";
      radio.value = String(index);
      radio.addEventListener("change", () => pick(camera));
      label.append(radio, element("strong", camera.model || "UniFi camera"), ` ${camera.ip}  ${camera.mac}`);
      const details = element("div", "", "hint");
      details.append(`Firmware ${camera.firmware || "unknown"} `);
      const fw = { tested: ["tested", "ok"], untested: ["untested", "warn"] }[camera.firmware_status] || ["unknown", "warn"];
      details.append(badge(fw[0], fw[1]), " ");
      if (camera.managed === true) details.append(badge("managed by a controller", "bad"));
      else if (camera.managed === false) details.append(badge("ready to adopt", "ok"));
      if (!camera.ptz) details.append(" ", badge("not a PTZ model", "warn"));
      item.append(label, details);
      if (camera.firmware_status !== "tested") item.append(element("p", camera.firmware_message, "hint"));
      list.append(item);
    });
    if (state.cameras.length === 1) {
      list.querySelector("input").checked = true;
      pick(state.cameras[0]);
    }
  }

  async function scan() {
    const status = $("#scan-status");
    const button = $("#scan");
    button.disabled = true;
    status.textContent = "Scanning…";
    try {
      state.cameras = (await call("POST", "api/setup/discover", {})).cameras || [];
      renderCameras();
      status.textContent = state.cameras.length
        ? `Found ${state.cameras.length} camera${state.cameras.length > 1 ? "s" : ""}. Choose one.`
        : "No camera answered. With bridge networking the scan cannot reach the LAN: type the camera's address below.";
    } catch (failure) {
      status.textContent = `The scan failed: ${failure.message}. Type the camera's address below.`;
    } finally {
      button.disabled = false;
    }
  }

  $("#scan").addEventListener("click", scan);

  $("#lookup").addEventListener("click", async () => {
    const ip = cameraForm.elements.ip.value.trim();
    const status = $("#scan-status");
    if (!ip) { cameraForm.elements.ip.focus(); return; }
    status.textContent = `Asking ${ip}…`;
    try {
      const found = (await call("POST", "api/setup/discover", { ip })).cameras || [];
      if (found.length) {
        state.cameras = found;
        renderCameras();
        status.textContent = `Found ${found[0].model} at ${found[0].ip}.`;
      } else {
        status.textContent = `Nothing answered at ${ip}. Check the address, or type the MAC address from the camera's label.`;
      }
    } catch (failure) {
      status.textContent = failure.message;
    }
  });

  // ------------------------------------------------------------ save + restart

  cameraForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    collect(event.target);
    delete state.values.pick;
    const button = event.target.querySelector("button[type=submit]");
    button.disabled = true;
    try {
      await save();
    } catch (failure) {
      show("camera");
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
      status.textContent = "Saved. Restart Osprey, then reload this page to continue.";
      return;
    }
    status.textContent = "Saved. Osprey is restarting with the new configuration…";
    if (await waitForRestart(state.boot)) {
      status.textContent = "Osprey restarted with the new configuration.";
      $("#saved-next").disabled = false;
    } else {
      status.textContent = "Osprey has not come back yet. Check its log, then reload this page.";
    }
  }

  async function continueAfterSave() {
    if (await ensureSession()) {
      await enterAdopt();
    } else {
      show("signin");
    }
  }

  $("#saved-next").addEventListener("click", () => continueAfterSave().catch((f) => error(f.message)));

  $("#signin-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      state.csrf = (await call("POST", "api/v1/session", { password: event.target.elements.password.value })).csrf;
      await enterAdopt();
    } catch (failure) {
      error(failure.message);
    }
  });

  // ------------------------------------------------------------------ adopt

  async function enterAdopt() {
    if (!state.values.ip) {
      const status = await call("GET", "api/v1/setup/status");
      const camera = (status.cameras || [])[0] || {};
      state.values.ip = camera.ip || "";
    }
    $("#adopt-ip").textContent = state.values.ip || "(no address saved)";
    show("adopt");
  }

  function step(list, text, kind) {
    const item = element("li", "", `check ${kind}`);
    item.append(element("span", { ok: "Done", bad: "Failed", warn: "Waiting" }[kind] || "", "mark"), element("span", text));
    list.append(item);
    return item;
  }

  function troubleshooting(list, anchor, text) {
    const item = element("li", "", "check bad");
    const link = element("a", text);
    link.href = DOCS + "troubleshooting.md#" + anchor;
    link.target = "_blank";
    link.rel = "noopener";
    item.append(element("span", "Help", "mark"), link);
    list.append(item);
  }

  $("#adopt-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.target;
    const list = $("#adopt-progress");
    const button = form.querySelector("button[type=submit]");
    list.replaceChildren();
    button.disabled = true;
    try {
      const login = Object.fromEntries(new FormData(form));
      const result = await call("POST", "api/v1/setup/adopt", { ip: state.values.ip, ...login });
      step(list, result.message, "ok");
      await watchAdoption(list);
    } catch (failure) {
      step(list, failure.message, "bad");
      troubleshooting(list, "the-camera-never-connects", "The camera never connects");
    } finally {
      button.disabled = false;
    }
  });

  async function watchAdoption(list, seconds = 120) {
    const waiting = step(list, "Waiting for the camera to connect and adopt…", "warn");
    const deadline = Date.now() + seconds * 1000;
    let adopted = false;
    while (Date.now() < deadline) {
      await sleep(2000);
      let checks = [];
      try { checks = (await call("GET", "api/v1/cameras/default/health")).checks || []; } catch { continue; }
      const adoption = checks.find((c) => c.id === "adoption");
      if (!adopted && adoption && adoption.status === "ok") {
        adopted = true;
        waiting.remove();
        step(list, adoption.detail, "ok");
        step(list, "Waiting for video…", "warn");
      }
      if (adopted && checks.some((c) => String(c.id).startsWith("track:") && c.status === "ok")) {
        list.lastChild.remove();
        step(list, "Video is flowing.", "ok");
        $("#adopt-next").disabled = false;
        return;
      }
    }
    if (adopted) {
      step(list, "The camera adopted, but no video arrived yet.", "bad");
      troubleshooting(list, "no-video", "No video");
      $("#adopt-next").disabled = false;
    } else {
      step(list, "The camera has not connected.", "bad");
      troubleshooting(list, "the-camera-never-connects", "The camera never connects");
    }
  }

  $("#adopt-next").addEventListener("click", () => enterFrigate().catch((f) => error(f.message)));

  // ---------------------------------------------------------------- Frigate

  const frigateForm = $("#frigate-form");

  async function loadBlock(name) {
    const query = name ? `?name=${encodeURIComponent(name)}` : "";
    const block = await call("GET", `api/v1/setup/frigate${query}`);
    $("#frigate-yaml").textContent = block.yaml;
    frigateForm.elements.name.value = block.name;
    if (!frigateForm.elements.url.value) frigateForm.elements.url.value = block.frigate_url || "";
  }

  async function enterFrigate() {
    await loadBlock("");
    show("frigate");
  }

  frigateForm.elements.name.addEventListener("change", () => {
    $("#frigate-preview").hidden = true;
    loadBlock(frigateForm.elements.name.value).catch((f) => error(f.message));
  });

  $("#copy-yaml").addEventListener("click", async () => {
    const text = $("#frigate-yaml").textContent;
    try {
      await navigator.clipboard.writeText(text);
      $("#copy-status").textContent = "Copied.";
    } catch {
      // The clipboard API needs HTTPS; over plain HTTP select the text instead.
      getSelection().selectAllChildren($("#frigate-yaml"));
      $("#copy-status").textContent = "Selected. Press Ctrl+C (or \u2318C) to copy.";
    }
  });

  function frigateRequest() {
    const data = Object.fromEntries(new FormData(frigateForm));
    data.insecure_tls = frigateForm.elements.insecure_tls.checked;
    return data;
  }

  function frigateFailure(failure) {
    const code = failure.data && failure.data.error && failure.data.error.code;
    if (code === "frigate_auth") $("#frigate-login").hidden = false;
    if (code === "frigate_tls") $("#frigate-tls").hidden = false;
    $("#frigate-result").textContent = failure.message;
  }

  frigateForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    $("#frigate-preview").hidden = true;
    $("#frigate-result").textContent = "Reading Frigate's config\u2026";
    try {
      const preview = await call("POST", "api/v1/setup/frigate/preview", frigateRequest());
      state.frigateBase = preview.base;
      $("#frigate-diff").textContent = preview.diff;
      $("#frigate-preview").hidden = false;
      $("#frigate-result").textContent = "";
    } catch (failure) {
      frigateFailure(failure);
    }
  });

  $("#frigate-apply").addEventListener("click", async () => {
    const button = $("#frigate-apply");
    button.disabled = true;
    $("#frigate-result").textContent = "Saving Frigate's config\u2026";
    try {
      const request = { ...frigateRequest(), base: state.frigateBase, restart: $("#frigate-restart").checked };
      const result = await call("POST", "api/v1/setup/frigate/apply", request);
      $("#frigate-preview").hidden = true;
      $("#frigate-result").textContent = `${result.message} The previous config is saved in ${result.backup}.`;
    } catch (failure) {
      frigateFailure(failure);
    } finally {
      button.disabled = false;
    }
  });

  $("#frigate-next").addEventListener("click", async () => {
    const url = frigateForm.elements.url.value.trim();
    // Keep a typed URL for the console and the health check even without Add to Frigate.
    if (url) await call("PUT", "api/v1/cameras/default/frigate", { base_url: url }).catch(() => {});
    next("frigate");
  });

  // ------------------------------------------------------------------ start

  async function start() {
    const status = $("#welcome-status");
    show("welcome");
    try {
      const info = await call("GET", "api/setup");
      $("#network-form [name=host]").value = info.host || "";
      $("#bridge-hint").hidden = !info.bridge_hint;
      status.textContent = "Ready.";
      $("section[data-step=welcome] [data-next]").disabled = false;
    } catch (failure) {
      if (failure.status === 404) {
        // Already saved: an admin can carry on with the steps after the restart.
        try {
          await continueAfterSave();
          return;
        } catch { /* show the message below */ }
        status.textContent = "Setup is already complete.";
      } else {
        status.textContent = `${failure.message}. Open the setup link from the Osprey log.`;
      }
    }
  }

  start();
})();
