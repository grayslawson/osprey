import { useEffect, useRef, useState } from "preact/hooks";
import { html } from "./html.js";
import { errorText, signIn } from "../actions.js";
import { ApiError } from "../api.js";

export function Locked() {
  return html`
    <main id="main" class="centered" tabindex="-1">
      <h1>Osprey is locked</h1>
      <div class="alert tone-bad" role="alert">
        <p>No admin password is configured, so the operator console stays closed.</p>
        <p>Set <code>OSPREY_ADMIN_PASSWORD_FILE</code> (or <code>OSPREY_ADMIN_PASSWORD_HASH</code>) and restart Osprey.</p>
        <p class="muted">Local development only: <code>OSPREY_ADMIN_AUTH=disabled</code> opens the console without a password.</p>
      </div>
    </main>`;
}

export function Login() {
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const input = useRef(null);

  useEffect(() => {
    input.current?.focus();
  }, []);

  async function submit(event) {
    event.preventDefault();
    const password = input.current.value;
    if (!password) {
      setError("Enter the admin password.");
      input.current.focus();
      return;
    }
    setBusy(true);
    setError("");
    try {
      // On success the shell replaces this form, so nothing is left to reset.
      await signIn(password);
    } catch (failure) {
      const message = failure instanceof ApiError && failure.status === 429
        ? failure.message
        : failure instanceof ApiError && failure.status === 401
          ? "That password is not correct."
          : errorText(failure);
      setError(message);
      input.current.select();
      input.current.focus();
    } finally {
      setBusy(false);
    }
  }

  return html`
    <main id="main" class="centered" tabindex="-1">
      <h1>Sign in to Osprey</h1>
      <p class="muted">Enter the operator password to control the camera.</p>
      <form onSubmit=${submit} noValidate>
        <label>Password
          <input ref=${input} name="password" type="password" autocomplete="current-password"
            aria-invalid=${error ? "true" : "false"} aria-describedby="login-error" />
        </label>
        <p id="login-error" class="field-error" role="alert">${error}</p>
        <button class="primary" type="submit" disabled=${busy}>${busy ? "Signing in…" : "Sign in"}</button>
      </form>
    </main>`;
}
