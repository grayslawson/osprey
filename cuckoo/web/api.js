// Thin client for /api/v1. Every URL is relative to the page, so the same
// bundle works at "/" and behind Home Assistant ingress ("/api/hassio_ingress/<id>/").
const ROOT = new URL(".", document.baseURI);

let csrfToken = "";

export class ApiError extends Error {
  constructor(status, code, message, hint = "") {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.hint = hint;
  }
}

export function setCsrf(token) {
  csrfToken = token || "";
}

export function pageUrl(path) {
  return new URL(path, ROOT).href;
}

export function cameraPath(cam, rest = "") {
  return `api/v1/cameras/${encodeURIComponent(cam)}${rest}`;
}

export async function api(method, path, body) {
  const headers = { Accept: "application/json" };
  const init = { method, headers, credentials: "same-origin", cache: "no-store" };
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  if (method !== "GET" && csrfToken) headers["X-CSRF-Token"] = csrfToken;
  let response;
  try {
    response = await fetch(pageUrl(path), init);
  } catch (cause) {
    throw new ApiError(0, "network", "Osprey cannot be reached.", "Check the connection and try again.");
  }
  const text = await response.text();
  let data = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch (cause) {
      data = null;
    }
  }
  if (!response.ok) {
    const error = (data && data.error) || {};
    throw new ApiError(
      response.status,
      error.code || "http",
      error.message || `Request failed (HTTP ${response.status}).`,
      error.hint || "",
    );
  }
  return data;
}

export const get = (path) => api("GET", path);
export const post = (path, body) => api("POST", path, body === undefined ? {} : body);
export const put = (path, body) => api("PUT", path, body);
export const del = (path) => api("DELETE", path);
