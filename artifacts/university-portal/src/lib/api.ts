const TOKEN_KEY = "uniportal_auth_token";

let _token: string | null = null;

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

export function fetchWithAuth(
  input: RequestInfo | URL,
  init?: RequestInit,
): Promise<Response> {
  const headers = new Headers(init?.headers);
  const tok = getToken();
  if (tok) {
    headers.set("Authorization", `Bearer ${tok}`);
  }
  return fetch(input, { ...init, credentials: "include", headers });
}
