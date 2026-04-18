/**
 * Parse JSON from a fetch Response without throwing on empty bodies.
 * Native `response.json()` throws "Unexpected end of JSON input" when the body is empty.
 */
export async function readResponseJson<T = unknown>(res: Response): Promise<T | null> {
  const raw = await res.text();
  const trimmed = raw.trim();
  if (trimmed === "") return null;
  try {
    return JSON.parse(trimmed) as T;
  } catch {
    return null;
  }
}

/**
 * Human-readable message for a failed fetch (non-2xx). Handles JSON `{ error }`, HTML 404 pages, and empty bodies.
 */
export async function getFetchErrorMessage(res: Response): Promise<string> {
  const raw = await res.text();
  const trimmed = raw.trim();
  const statusBit = `${res.status} ${res.statusText || ""}`.trim();

  if (!trimmed) {
    return (
      `Request failed (${statusBit}). Empty response — is the API running and is /api proxied to it? ` +
      `(Local dev: run the API and set API_PROXY_TARGET if not http://localhost:8080.)`
    );
  }

  try {
    const j = JSON.parse(trimmed) as { error?: string; message?: string; detail?: string };
    if (typeof j.error === "string" && j.error.trim()) return j.error.trim();
    if (typeof j.message === "string" && j.message.trim()) return j.message.trim();
    if (typeof j.detail === "string" && j.detail.trim()) return j.detail.trim();
  } catch {
    // not JSON (often HTML from a 404/502 page)
  }

  const snippet = trimmed.replace(/\s+/g, " ").slice(0, 280);
  return `Request failed (${statusBit}): ${snippet}`;
}
