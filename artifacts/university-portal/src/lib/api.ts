const TOKEN_KEY = "uniportal_auth_token";

let _token: string | null = null;
let _onUnauthorized: (() => void) | null = null;

/**
 * Register a callback invoked whenever `fetchWithAuth` receives a
 * `401 Unauthorized` response (e.g. an expired/invalid session cookie).
 * Used to trigger a global logout + redirect-to-login instead of letting
 * every individual page show its own raw "Invalid session" error.
 * Pass `null` to clear the handler.
 */
export function setOnUnauthorized(handler: (() => void) | null): void {
  _onUnauthorized = handler;
}

export function setAuthToken(token: string | null): void {
  _token = token;
  try {
    if (token) {
      localStorage.setItem(TOKEN_KEY, token);
    } else {
      localStorage.removeItem(TOKEN_KEY);
    }
  } catch {
  }
}

export function loadAuthToken(): string | null {
  try {
    _token = localStorage.getItem(TOKEN_KEY);
  } catch {
    _token = null;
  }
  return _token;
}

function getToken(): string | null {
  if (_token) return _token;
  return loadAuthToken();
}

export async function fetchWithAuth(
  input: RequestInfo | URL,
  init?: RequestInit,
): Promise<Response> {
  const headers = new Headers(init?.headers);
  const tok = getToken();
  if (tok) {
    headers.set("Authorization", `Bearer ${tok}`);
  }
  const res = await fetch(input, { ...init, credentials: "include", headers });
  if (res.status === 401) {
    _onUnauthorized?.();
  }
  return res;
}
